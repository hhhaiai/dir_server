import os
import sys
import http.server
import socketserver
import socket
from urllib.parse import unquote, parse_qs
from html import escape
import io
import logging
import shutil
from zipfile import ZipFile
from datetime import datetime
import threading
import psutil
import argparse

PAGE_SIZE = 20  # 每页显示的文件数

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
    except Exception as e:
        logging.error(f"Failed to get local IP address: {e}")
        local_ip = '127.0.0.1'
    finally:
        s.close()
    return local_ip

def kill_process_on_port(port):
    try:
        for proc in psutil.process_iter(['pid', 'name']):
            for conn in proc.connections(kind='inet'):
                if conn.laddr.port == port and proc.info['pid'] > 100:  # 避免终止系统进程
                    proc.terminate()
                    proc.wait(timeout=3)
                    if proc.is_running():
                        proc.kill()
                    logging.info(f"Killed process {proc.info['name']} (PID: {proc.info['pid']}) on port {port}")
    except psutil.NoSuchProcess:
        logging.info(f"No process found on port {port}")
    except Exception as e:
        logging.error(f"Failed to kill process on port {port}: {e}")

class CustomHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    zip_creation_lock = threading.Lock()

    def list_directory(self, path):
        try:
            entries = os.listdir(path)
        except os.error as e:
            logging.error(f"Failed to list directory {path}: {e}")
            self.send_error(http.HTTPStatus.NOT_FOUND, "No permission to list directory")
            return None

        query = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
        page = int(query.get('page', [1])[0])
        search = query.get('search', [''])[0].lower()

        if search:
            entries = [entry for entry in entries if search in entry.lower()]

        entries.sort(key=lambda a: a.lower())
        total_pages = (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE
        entries = entries[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

        r = self.generate_html_list(path, entries, page, total_pages, search)
        encoded = r.encode(sys.getfilesystemencoding(), 'surrogateescape')
        f = io.BytesIO()
        f.write(encoded)
        f.seek(0)
        self.send_response(http.HTTPStatus.OK)
        self.send_header("Content-type", "text/html; charset=%s" % sys.getfilesystemencoding())
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        return f

    def generate_html_list(self, path, entries, page, total_pages, search):
        try:
            displaypath = unquote(self.path)
        except UnicodeDecodeError:
            displaypath = unquote(self.path.encode('ascii', 'surrogateescape'))
        displaypath = escape(displaypath, quote=False)
        enc = sys.getfilesystemencoding()
        title = 'FTP共享文件/ 的索引'
        r = []
        r.append('<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN" '
                 '"http://www.w3.org/TR/html4/strict.dtd">')
        r.append('<html>\n<head>')
        r.append('<meta http-equiv="Content-Type" '
                 'content="text/html; charset=%s">' % enc)
        r.append('<title>%s</title>' % title)
        r.append('<style>')
        r.append('body { font-family: Arial, sans-serif; background-color: #f5f5f5; }')
        r.append('h1 { font-size: 24px; text-align: center; }')
        r.append('ul { list-style-type: none; padding: 0; width: 80%; margin: auto; }')
        r.append('li { display: flex; justify-content: space-between; align-items: center; '
                 'background-color: #ffffff; padding: 10px; margin: 5px 0; border-radius: 5px; box-shadow: 0 0 5px rgba(0, 0, 0, 0.1); }')
        r.append('li.header { background-color: #333333; color: white; font-weight: bold; box-shadow: none; }')
        r.append('li.header span { color: white; }')
        r.append('a { text-decoration: none; color: #333333; font-weight: bold; }')
        r.append('a:hover { text-decoration: underline; }')
        r.append('.size { color: #666666; }')
        r.append('.date { color: #999999; font-size: 12px; }')
        r.append('.actions { color: #007BFF; }')
        r.append('.actions a { color: #007BFF; }')
        r.append('.actions a:hover { text-decoration: underline; }')
        r.append('.search { text-align: center; margin: 20px 0; }')
        r.append('.pagination { text-align: center; margin: 20px 0; }')
        r.append('.pagination a { margin: 0 5px; text-decoration: none; color: #007BFF; }')
        r.append('.pagination a:hover { text-decoration: underline; }')
        r.append('</style>\n</head>')
        r.append('<body>\n<h1>%s</h1>' % title)
        r.append('<div class="search"><form method="get">')
        r.append('<input type="text" name="search" value="%s">' % escape(search))
        r.append('<input type="submit" value="搜索">')
        r.append('</form></div>')
        r.append('<ul>')

        r.append('<li><a href="..">[父目录]</a></li>')

        r.append('<li class="header">'
                 '<span>名称</span>'
                 '<span class="size">大小</span>'
                 '<span class="date">修改日期</span>'
                 '<span class="actions">操作</span>'
                 '</li>')

        for name in entries:
            fullname = os.path.join(path, name)
            displayname = linkname = name
            mod_time = self.get_modification_date(fullname)
            if os.path.isdir(fullname):
                folder_size = self.get_directory_size(fullname)
                size_str = self.human_readable_size(folder_size)
                zip_link = linkname + ".zip"
                r.append(self.generate_directory_entry(linkname, displayname, size_str, mod_time))
            else:
                size = os.path.getsize(fullname)
                size_str = self.human_readable_size(size)
                r.append(self.generate_file_entry(linkname, displayname, size_str, mod_time))

        r.append('</ul>')

        r.append('<div class="pagination">')
        if page > 1:
            r.append('<a href="?page=%d&search=%s">上一页</a>' % (page - 1, escape(search)))
        if page < total_pages:
            r.append('<a href="?page=%d&search=%s">下一页</a>' % (page + 1, escape(search)))
        r.append('</div>')

        r.append('</body>\n</html>\n')
        return '\n'.join(r)

    def generate_directory_entry(self, linkname, displayname, size_str, mod_time):
        return (f'<li><a href="{linkname}/">{displayname}/</a>'
                f'<span class="size">{size_str}</span>'
                f'<span class="date">{mod_time}</span>'
                f'<span class="actions"><a href="{linkname}.zip">下载</a></span></li>')

    def generate_file_entry(self, linkname, displayname, size_str, mod_time):
        download_link = linkname.replace(" ", "%20")
        preview_link = linkname.replace(" ", "%20")  # 预览链接

        # 定义可预览的文件扩展名
        previewable_extensions = [
            '.txt', '.csv', '.log', '.md', '.json', '.xml',
            '.yaml', '.yml', '.ini',
            '.java', '.cpp', '.c', '.cs', '.js',
            '.html', '.css', '.py', '.rb', '.php',
            '.go', '.swift', '.ts',
            '.bash', '.sh', '.zsh'
        ]

        # 检查文件扩展名是否在可预览列表中
        if any(displayname.endswith(ext) for ext in previewable_extensions):
            preview_link_html = f'<a href="{preview_link}" target="_blank">预览</a>'
        else:
            preview_link_html = ''  # 不显示预览链接

        return (f'<li><a href="{preview_link}" target="_blank">{displayname}</a>'
                f'<span class="size">{size_str}</span>'
                f'<span class="date">{mod_time}</span>'
                f'<span class="actions">'
                f'<a href="{download_link}" download>下载</a>'
                f' {preview_link_html}</span></li>')

    def human_readable_size(self, size, decimal_places=2):
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024:
                return f"{size:.{decimal_places}f} {unit}"
            size /= 1024

    def get_modification_date(self, path):
        mod_time = os.path.getmtime(path)
        return datetime.fromtimestamp(mod_time).strftime('%Y-%m-%d %H:%M:%S')

    def get_directory_size(self, path):
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                total_size += os.path.getsize(fp)
        return total_size

    def create_zip(self, directory, zip_name):
        with self.zip_creation_lock:
            if not os.path.exists(zip_name):
                with ZipFile(zip_name, 'w') as zipf:
                    for foldername, subfolders, filenames in os.walk(directory):
                        for filename in filenames:
                            filepath = os.path.join(foldername, filename)
                            arcname = os.path.relpath(filepath, os.path.dirname(directory))
                            zipf.write(filepath, arcname)

    def do_GET(self):
        # 获取文件系统路径
        file_path = self.translate_path(self.path)

        # 检查请求的路径是否是一个现有的.zip文件
        if self.path.endswith(".zip") and os.path.isfile(file_path):
            # 如果是现有的.zip文件，直接提供下载
            try:
                with open(file_path, 'rb') as f:
                    self.send_response(http.HTTPStatus.OK)
                    self.send_header("Content-type", "application/zip")
                    self.send_header("Content-Length", str(os.path.getsize(file_path)))
                    self.end_headers()
                    shutil.copyfileobj(f, self.wfile)
            except IOError:
                self.send_error(http.HTTPStatus.NOT_FOUND, "File not found")
            except BrokenPipeError:
                logging.error("Broken pipe error while sending file")
            return

        # 如果请求的路径是目录，则处理为目录压缩请求
        if self.path.endswith(".zip"):
            directory = self.path[:-4]
            directory_path = self.translate_path(directory)
            zip_path = self.translate_path(self.path)

            if os.path.isdir(directory_path):
                if not os.path.exists(zip_path):
                    self.create_zip(directory_path, zip_path)

                try:
                    with open(zip_path, 'rb') as f:
                        self.send_response(http.HTTPStatus.OK)
                        self.send_header("Content-type", "application/zip")
                        self.send_header("Content-Length", str(os.path.getsize(zip_path)))
                        self.end_headers()
                        shutil.copyfileobj(f, self.wfile)
                except BrokenPipeError:
                    logging.error("Broken pipe error while sending file")
                finally:
                    os.remove(zip_path)
            else:
                self.send_error(http.HTTPStatus.NOT_FOUND, "Directory not found")
        elif self.path.endswith(".txt") or self.path.endswith(".csv"):
            self.handle_text_file()
        else:
            super().do_GET()

    def handle_text_file(self):
        try:
            file_path = self.translate_path(self.path)

            self.send_response(http.HTTPStatus.OK)
            self.send_header("Content-type", f"text/plain; charset=utf-8")
            self.end_headers()

            with open(file_path, 'r', encoding='utf-8', newline='') as file:
                while True:
                    chunk = file.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk.encode('utf-8'))
        except IOError:
            self.send_error(http.HTTPStatus.NOT_FOUND, "File not found")
        except BrokenPipeError:
            logging.error("Broken pipe error while sending file")
        except Exception as e:
            logging.error(f"Unexpected error: {e}")

def start_serve(shared_directory=None, save_logs=False, initial_port=8000):
    # 如果没有提供共享目录，使用桌面目录
    if not shared_directory:
        if os.name == 'nt':  # Windows
            shared_directory = os.path.join(os.environ['USERPROFILE'], 'Desktop')
        else:  # macOS/Linux
            shared_directory = os.path.join(os.path.expanduser('~'), 'Desktop')

    log_filename = f'server_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log' if save_logs else None
    logging.basicConfig(level=logging.INFO, filename=log_filename,
                        format='%(asctime)s - %(levelname)s - %(message)s')
    ip = get_local_ip()
    port = initial_port
    os.chdir(shared_directory)

    try:
        kill_process_on_port(port)
    except Exception as e:
        logging.error(f"Failed to kill process on port {port}: {e}")
        port += 1

    Handler = CustomHTTPRequestHandler

    while True:
        try:
            with socketserver.ThreadingTCPServer(("0.0.0.0", port), Handler) as httpd:
                logging.info(f"Serving at http://{ip}:{port}")
                print(f"Serving at http://{ip}:{port}")
                httpd.serve_forever()
                break
        except OSError as e:
            if e.errno == 98:  # Address already in use
                logging.info(f"Port {port} is in use, trying next port...")
                port += 1
                try:
                    kill_process_on_port(port)
                except Exception as e:
                    logging.error(f"Failed to kill process on port {port}: {e}")
            else:
                logging.error(f"Failed to start server: {e}")
                raise

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Start a simple HTTP server.')
    parser.add_argument('shared_directory', nargs='?', default=None, help='The directory to share. Defaults to Desktop if not provided.')
    parser.add_argument('--logs', action='store_true', help='Enable logging to a file.')
    args = parser.parse_args()

    start_serve(args.shared_directory, args.logs)
