import os
import sys
import http.server
import socketserver
import socket
from urllib.parse import unquote, parse_qs, quote
from html import escape
import io
import logging
import shutil
from zipfile import ZipFile
from datetime import datetime
import threading
import psutil
import argparse
import hashlib
import time
import mimetypes
from pathlib import Path
from functools import lru_cache

# --- Configuration ---
PAGE_SIZE = 20  # 每页显示的文件数
DIRECTORY_SIZE_CACHE_TTL = 300  # 目录大小缓存时间 (秒)

# --- Setup Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__) # 使用命名 logger

# --- Utility Functions ---

def get_local_ip() -> str:
    """获取本机IP地址"""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 不需要真实连接，只是获取路由信息
        s.connect(('10.255.255.255', 1)) # 使用一个不存在的地址
        local_ip = s.getsockname()[0]
    except Exception as e:
        logger.error(f"Failed to get local IP address: {e}")
        local_ip = '127.0.0.1'
    finally:
        if s:
            s.close()
    return local_ip

def kill_process_on_port(port: int) -> None:
    """尝试终止占用指定端口的进程"""
    try:
        for proc in psutil.process_iter(['pid', 'name']):
            try: # 再次检查进程信息，防止进程在迭代中消失
                for conn in proc.connections(kind='inet'):
                    if conn.laddr.port == port and proc.info['pid'] > 1: # 避免终止系统关键进程
                        logger.info(f"Terminating process {proc.info['name']} (PID: {proc.info['pid']}) on port {port}")
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except psutil.TimeoutExpired:
                            logger.warning(f"Process {proc.info['pid']} did not terminate gracefully, killing it.")
                            proc.kill()
                        logger.info(f"Process on port {port} terminated.")
                        return # 找到并终止后退出
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # 进程可能已结束或无权限访问，继续下一个
                pass
    except Exception as e:
        logger.error(f"Error while trying to kill process on port {port}: {e}")

def get_content_type(path: str) -> str:
    """根据文件扩展名猜测 Content-Type"""
    content_type, _ = mimetypes.guess_type(path)
    # 如果猜不到，默认为 application/octet-stream (二进制流，浏览器通常会提示下载)
    return content_type or 'application/octet-stream'

# --- Main Handler Class ---

class CustomHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """
    自定义HTTP请求处理器，用于文件浏览和共享。
    """
    zip_creation_lock = threading.Lock()
    # 使用 lru_cache 缓存目录大小，带 TTL
    @lru_cache(maxsize=128)
    def _cached_get_directory_size(self, path: str, mtime: float) -> int:
        """内部方法，用于缓存目录大小。mtime 作为缓存键的一部分，用于失效检查。"""
        logger.debug(f"Calculating size for directory: {path}")
        total_size = 0
        try:
            path_obj = Path(path)
            for fp in path_obj.rglob('*'):
                if fp.is_file():
                    total_size += fp.stat().st_size
        except OSError as e:
            logger.error(f"Error calculating directory size for {path}: {e}")
        return total_size

    def get_directory_size(self, path: str) -> int:
        """获取目录大小，使用带失效检查的缓存。"""
        try:
            mtime = Path(path).stat().st_mtime
        except OSError as e:
            logger.error(f"Error getting mtime for {path}: {e}")
            return 0 # 或者返回一个默认值/错误标识
        
        # 使用路径和修改时间的哈希作为缓存键的一部分，确保内容改变时缓存失效
        # 注意：lru_cache 本身不直接支持基于时间的失效，这里依赖 mtime 变化来触发重新计算
        # 更复杂的场景可能需要自定义缓存或使用如 `cachetools` 库
        cache_key_path = str(Path(path).resolve()) # 使用绝对路径作为键的一部分
        return self._cached_get_directory_size(cache_key_path, mtime)

    def list_directory(self, path: str):
        """列出目录内容，支持分页和搜索。"""
        try:
            # 使用 pathlib 处理路径
            path_obj = Path(path)
            entries = [p.name for p in path_obj.iterdir()]
        except OSError as e:
            logger.error(f"Failed to list directory {path}: {e}")
            self.send_error(http.HTTPStatus.NOT_FOUND, "No permission to list directory")
            return None

        # 解析查询参数
        query_components = parse_qs(self.path.split('?')[-1]) if '?' in self.path else {}
        page = int(query_components.get('page', [1])[0])
        search = query_components.get('search', [''])[0].lower()

        # 应用搜索过滤
        if search:
            entries = [entry for entry in entries if search in entry.lower()]

        # 排序
        entries.sort(key=lambda a: a.lower())
        
        # 计算分页
        total_entries = len(entries)
        total_pages = max(1, (total_entries + PAGE_SIZE - 1) // PAGE_SIZE) # 至少1页
        page = max(1, min(page, total_pages)) # 确保页码在有效范围内
        start_idx = (page - 1) * PAGE_SIZE
        end_idx = start_idx + PAGE_SIZE
        entries = entries[start_idx:end_idx]

        # 生成 HTML
        r = self.generate_html_list(str(path_obj), entries, page, total_pages, search)
        
        # 发送响应
        encoded = r.encode(sys.getfilesystemencoding(), 'surrogateescape')
        f = io.BytesIO()
        f.write(encoded)
        f.seek(0)
        self.send_response(http.HTTPStatus.OK)
        self.send_header("Content-type", "text/html; charset=%s" % sys.getfilesystemencoding())
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        return f

    def generate_html_list(self, path: str, entries: list, page: int, total_pages: int, search: str) -> str:
        """生成目录列表的 HTML 页面。"""
        try:
            displaypath = unquote(self.path, errors='surrogatepass') # 更安全的解码
        except UnicodeDecodeError:
            displaypath = unquote(self.path.encode('utf-8', 'surrogateescape').decode('utf-8', 'replace'))
        displaypath = escape(displaypath, quote=False)
        enc = sys.getfilesystemencoding()
        title = '文件共享目录索引'

        # 使用多行字符串定义 HTML 模板，提高可读性
        html_template = '''<!DOCTYPE HTML>
<html>
<head>
    <meta http-equiv="Content-Type" content="text/html; charset={charset}">
    <title>{title}</title>
    <style>
        body {{ font-family: Arial, sans-serif; background-color: #f5f5f5; margin: 0; padding: 20px; }}
        h1 {{ font-size: 24px; text-align: center; color: #333; }}
        .search {{ text-align: center; margin: 20px 0; }}
        .search input[type="text"] {{ padding: 8px; width: 300px; border: 1px solid #ccc; border-radius: 4px; }}
        .search input[type="submit"] {{ padding: 8px 15px; background-color: #007BFF; color: white; border: none; border-radius: 4px; cursor: pointer; }}
        .search input[type="submit"]:hover {{ background-color: #0056b3; }}
        ul {{ list-style-type: none; padding: 0; width: 90%; max-width: 1200px; margin: 20px auto; }}
        li {{ display: flex; justify-content: space-between; align-items: center; 
              background-color: #ffffff; padding: 12px; margin: 8px 0; border-radius: 6px; 
              box-shadow: 0 2px 4px rgba(0,0,0,0.1); transition: box-shadow 0.2s; }}
        li:hover {{ box-shadow: 0 4px 8px rgba(0,0,0,0.15); }}
        li.header {{ background-color: #343a40; color: white; font-weight: bold; box-shadow: none; }}
        li.header span {{ color: white; }}
        a {{ text-decoration: none; color: #007BFF; font-weight: bold; }}
        a:hover {{ text-decoration: underline; }}
        .size, .date {{ color: #6c757d; }}
        .actions {{ color: #28a745; }}
        .actions a {{ color: #28a745; margin: 0 5px; }}
        .actions a:hover {{ color: #1e7e34; }}
        .pagination {{ text-align: center; margin: 30px 0; }}
        .pagination a, .pagination span {{ 
            display: inline-block; padding: 8px 12px; margin: 0 4px; 
            text-decoration: none; color: #007BFF; border: 1px solid #dee2e6; border-radius: 4px; 
        }}
        .pagination a:hover {{ background-color: #e9ecef; }}
        .pagination .current {{ background-color: #007BFF; color: white; border-color: #007BFF; }}
        .pagination .disabled {{ color: #6c757d; cursor: not-allowed; border-color: #dee2e6; }}
        .back-link {{ display: block; text-align: center; margin-top: 20px; }}
    </style>
</head>
<body>
    <h1>{title}</h1>
    <div class="search">
        <form method="get">
            <input type="text" name="search" value="{escaped_search}" placeholder="搜索文件/文件夹...">
            <input type="submit" value="搜索">
        </form>
    </div>
    <ul>
        <li class="header">
            <span>名称</span>
            <span class="size">大小</span>
            <span class="date">修改日期</span>
            <span class="actions">操作</span>
        </li>
        <li><a href="..">[返回上级目录]</a></li>
'''
        # 填充模板
        html_parts = [html_template.format(
            charset=enc,
            title=escape(title),
            escaped_search=escape(search)
        )]

        # 生成文件/目录条目
        path_obj = Path(path)
        for name in entries:
            fullname = path_obj / name
            displayname = linkname = name
            try:
                stat_result = fullname.stat()
                mod_time_str = datetime.fromtimestamp(stat_result.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            except OSError as e:
                logger.warning(f"Could not get stats for {fullname}: {e}")
                mod_time_str = "N/A"

            if fullname.is_dir():
                folder_size = self.get_directory_size(str(fullname))
                size_str = self.human_readable_size(folder_size)
                html_parts.append(self.generate_directory_entry(linkname, displayname, size_str, mod_time_str))
            else:
                try:
                    size = stat_result.st_size # 使用之前获取的 stat
                    size_str = self.human_readable_size(size)
                except OSError as e:
                    logger.warning(f"Could not get size for {fullname}: {e}")
                    size_str = "N/A"
                html_parts.append(self.generate_file_entry(linkname, displayname, size_str, mod_time_str))

        html_parts.append('</ul>')

        # 生成分页导航
        html_parts.append('<div class="pagination">')
        if page > 1:
            html_parts.append(f'<a href="?page={page-1}&search={quote(search)}">&laquo; 上一页</a>')
        else:
            html_parts.append('<span class="disabled">&laquo; 上一页</span>')

        # 显示页码 (简化版，只显示当前页前后几页)
        start_page = max(1, page - 2)
        end_page = min(total_pages, page + 2)
        if start_page > 1:
             html_parts.append('<a href="?page=1&search={}">1</a>'.format(quote(search)))
             if start_page > 2:
                 html_parts.append('<span>...</span>')
        for p in range(start_page, end_page + 1):
            if p == page:
                html_parts.append(f'<span class="current">{p}</span>')
            else:
                html_parts.append(f'<a href="?page={p}&search={quote(search)}">{p}</a>')
        if end_page < total_pages:
            if end_page < total_pages - 1:
                 html_parts.append('<span>...</span>')
            html_parts.append('<a href="?page={}&search={}">{}</a>'.format(total_pages, quote(search), total_pages))

        if page < total_pages:
            html_parts.append(f'<a href="?page={page+1}&search={quote(search)}">下一页 &raquo;</a>')
        else:
            html_parts.append('<span class="disabled">下一页 &raquo;</span>')
        html_parts.append('</div>')
        html_parts.append('<div class="back-link"><a href="/">回到根目录</a></div>')
        html_parts.append('</body>\n</html>\n')
        return ''.join(html_parts)

    def generate_directory_entry(self, linkname: str, displayname: str, size_str: str, mod_time_str: str) -> str:
        """生成目录条目的 HTML。"""
        safe_linkname = escape(linkname, quote=True)
        safe_displayname = escape(displayname, quote=False)
        return (f'<li><a href="{safe_linkname}/">{safe_displayname}/</a>'
                f'<span class="size">{size_str}</span>'
                f'<span class="date">{mod_time_str}</span>'
                f'<span class="actions"><a href="{safe_linkname}.zip">下载 (ZIP)</a></span></li>')

    def generate_file_entry(self, linkname: str, displayname: str, size_str: str, mod_time_str: str) -> str:
        """生成文件条目的 HTML。"""
        safe_linkname = escape(linkname, quote=True)
        safe_displayname = escape(displayname, quote=False)
        
        # 定义可预览的文件扩展名 (MIME types 通常更好，但这里简化处理)
        previewable_extensions = {
            '.txt', '.csv', '.log', '.md', '.json', '.xml',
            '.yaml', '.yml', '.ini', '.cfg',
            '.py', '.js', '.html', '.css', '.java', '.c', '.cpp', '.h', '.cs',
            '.rb', '.php', '.go', '.swift', '.ts', '.sh', '.bash', '.sql'
        }
        
        preview_link_html = ''
        _, ext = os.path.splitext(displayname.lower())
        if ext in previewable_extensions:
            preview_link_html = f' <a href="{safe_linkname}" target="_blank">预览</a>'

        return (f'<li><a href="{safe_linkname}">{safe_displayname}</a>'
                f'<span class="size">{size_str}</span>'
                f'<span class="date">{mod_time_str}</span>'
                f'<span class="actions"><a href="{safe_linkname}" download>下载</a>{preview_link_html}</span></li>')

    def human_readable_size(self, size: int, decimal_places: int = 2) -> str:
        """将字节大小转换为可读格式。"""
        if size < 0:
            return "N/A"
        for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
            if size < 1024.0:
                return f"{size:.{decimal_places}f} {unit}"
            size /= 1024.0
        return f"{size:.{decimal_places}f} EB" # 处理极大文件

    def create_zip(self, directory: str, zip_name: str) -> bool:
        """创建目录的 ZIP 压缩包。"""
        with self.zip_creation_lock:
            if os.path.exists(zip_name):
                logger.info(f"ZIP file {zip_name} already exists.")
                return True # 文件已存在，视为成功
            try:
                logger.info(f"Creating ZIP file {zip_name} for directory {directory}")
                directory_path = Path(directory)
                zip_path = Path(zip_name)
                
                # 使用临时文件名创建，完成后再重命名，提高原子性
                temp_zip_path = zip_path.with_suffix(zip_path.suffix + f".tmp_{os.getpid()}")
                
                with ZipFile(temp_zip_path, 'w') as zipf:
                    # rglob('*') 递归获取所有文件和目录
                    for file_path in directory_path.rglob('*'):
                        if file_path.is_file():
                            # 计算存档内的相对路径
                            arcname = file_path.relative_to(directory_path)
                            zipf.write(file_path, arcname)
                
                # 原子性地移动临时文件
                shutil.move(str(temp_zip_path), str(zip_path))
                logger.info(f"Successfully created ZIP file {zip_name}")
                return True
            except Exception as e:
                logger.error(f"Failed to create ZIP file {zip_name}: {e}")
                # 清理可能残留的临时文件
                try:
                    if temp_zip_path.exists():
                        temp_zip_path.unlink()
                except Exception as cleanup_error:
                    logger.error(f"Failed to clean up temp ZIP file {temp_zip_path}: {cleanup_error}")
                return False

    def do_GET(self):
        """处理 GET 请求。"""
        # 使用 pathlib 处理路径
        full_path = Path(self.translate_path(self.path))
        logger.info(f"GET request for: {self.path} -> {full_path}")

        # 检查是否请求现有的 ZIP 文件
        if self.path.endswith(".zip") and full_path.is_file():
            logger.debug(f"Serving existing ZIP file: {full_path}")
            try:
                fs = full_path.stat()
                with open(full_path, 'rb') as f:
                    self.send_response(http.HTTPStatus.OK)
                    self.send_header("Content-type", "application/zip")
                    self.send_header("Content-Length", str(fs.st_size))
                    # 添加缓存控制头 (可选)
                    # self.send_header("Cache-Control", "public, max-age=3600")
                    self.end_headers()
                    shutil.copyfileobj(f, self.wfile)
                return
            except IOError as e:
                logger.error(f"IOError serving existing ZIP {full_path}: {e}")
                self.send_error(http.HTTPStatus.NOT_FOUND, "File not found")
                return
            except BrokenPipeError:
                logger.warning("Broken pipe error while sending existing ZIP file")
                return # Client disconnected, nothing to do

        # 检查是否是动态 ZIP 请求 (.zip 但对应目录存在)
        if self.path.endswith(".zip"):
            directory_rel_path = self.path[:-4] # 移除 .zip 后缀
            directory_full_path = Path(self.translate_path(directory_rel_path))
            
            if directory_full_path.is_dir():
                zip_full_path = full_path # full_path 已经是带 .zip 的完整路径了
                logger.debug(f"Dynamic ZIP request for directory: {directory_full_path}")
                
                if self.create_zip(str(directory_full_path), str(zip_full_path)):
                    if zip_full_path.is_file():
                        try:
                            fs = zip_full_path.stat()
                            with open(zip_full_path, 'rb') as f:
                                self.send_response(http.HTTPStatus.OK)
                                self.send_header("Content-type", "application/zip")
                                self.send_header("Content-Length", str(fs.st_size))
                                self.end_headers()
                                shutil.copyfileobj(f, self.wfile)
                        except BrokenPipeError:
                            logger.warning("Broken pipe error while sending dynamic ZIP file")
                        except Exception as e:
                            logger.error(f"Error sending dynamic ZIP {zip_full_path}: {e}")
                            self.send_error(http.HTTPStatus.INTERNAL_SERVER_ERROR, "Error sending ZIP file")
                        finally:
                            # 可选：删除临时 ZIP 文件以节省空间
                            # 注意：如果多个用户同时下载同一个目录，这可能会有问题
                            # 更好的做法是定期清理或使用缓存策略
                            try:
                                # zip_full_path.unlink() # Uncomment to delete after send
                                pass
                            except Exception as e:
                                logger.warning(f"Could not delete temporary ZIP {zip_full_path}: {e}")
                    else:
                        logger.error(f"ZIP file was not created at {zip_full_path}")
                        self.send_error(http.HTTPStatus.INTERNAL_SERVER_ERROR, "Failed to create ZIP file")
                else:
                    self.send_error(http.HTTPStatus.INTERNAL_SERVER_ERROR, "Failed to create ZIP file")
                return
            else:
                logger.warning(f"Directory not found for ZIP request: {directory_full_path}")
                self.send_error(http.HTTPStatus.NOT_FOUND, "Directory not found")
                return

        # 检查是否是可预览的文本文件
        # 使用 mimetypes 或简单扩展名检查
        mime_type = get_content_type(str(full_path))
        if mime_type.startswith('text/') or mime_type in ['application/json', 'application/xml']:
             if full_path.is_file():
                logger.debug(f"Serving text file for preview: {full_path}")
                self.handle_text_file(str(full_path), mime_type)
                return

        # 默认行为：提供静态文件或列出目录
        logger.debug(f"Delegating to parent class for: {full_path}")
        super().do_GET()

    def handle_text_file(self, file_path: str, mime_type: str):
        """处理文本文件的预览。"""
        try:
            # 尝试 UTF-8，失败则尝试其他编码或二进制
            encodings_to_try = ['utf-8', 'gbk', 'latin-1'] # 常见编码
            content = None
            used_encoding = None
            for enc in encodings_to_try:
                try:
                    with open(file_path, 'r', encoding=enc) as f:
                        content = f.read()
                        used_encoding = enc
                        break # 成功读取则跳出循环
                except UnicodeDecodeError:
                    continue # 尝试下一个编码
            
            if content is None:
                 # 如果所有文本编码都失败，以二进制形式发送
                 logger.warning(f"Could not decode {file_path} as text, sending as binary.")
                 self.send_error(http.HTTPStatus.NOT_FOUND, "File not found or not text") # 或者发送为 application/octet-stream
                 return

            self.send_response(http.HTTPStatus.OK)
            # 发送正确的 MIME 类型和字符集
            self.send_header("Content-type", f"{mime_type}; charset={used_encoding}")
            # 可选：添加缓存头
            # self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            # 编码后发送
            self.wfile.write(content.encode(used_encoding, errors='replace')) 
            
        except IOError as e:
            logger.error(f"IOError handling text file {file_path}: {e}")
            self.send_error(http.HTTPStatus.NOT_FOUND, "File not found")
        except BrokenPipeError:
            logger.warning("Broken pipe error while sending text file")
        except Exception as e:
            logger.error(f"Unexpected error handling text file {file_path}: {e}")
            self.send_error(http.HTTPStatus.INTERNAL_SERVER_ERROR, "Error reading file")


def start_serve(shared_directory: str = None, save_logs: bool = False, initial_port: int = 8000):
    """启动 HTTP 服务器。"""
    # 确定共享目录
    if not shared_directory:
        if os.name == 'nt':  # Windows
            shared_directory = os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop')
        else:  # macOS/Linux
            shared_directory = os.path.join(os.path.expanduser('~'), 'Desktop')
    
    # 确保共享目录存在
    shared_path = Path(shared_directory)
    if not shared_path.exists():
        logger.error(f"Shared directory does not exist: {shared_directory}")
        sys.exit(1)
    if not shared_path.is_dir():
        logger.error(f"Shared path is not a directory: {shared_directory}")
        sys.exit(1)

    # 配置日志
    if save_logs:
        log_filename = f'server_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
        file_handler = logging.FileHandler(log_filename)
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        logger.addHandler(file_handler)
        logger.info(f"Logging to file: {log_filename}")

    os.chdir(str(shared_path)) # 切换工作目录
    ip = get_local_ip()
    port = initial_port

    # 尝试杀死占用端口的进程
    try:
        kill_process_on_port(port)
    except Exception as e:
        logger.warning(f"Could not kill process on initial port {port}: {e}. Trying next port.")

    Handler = CustomHTTPRequestHandler

    # 循环查找可用端口
    while True:
        try:
            with socketserver.TCPServer(("", port), Handler) as httpd:
                logger.info(f"Starting server at http://{ip}:{port}")
                print(f"Serving at http://{ip}:{port}")
                httpd.serve_forever()
                break # 正常退出循环
        except OSError as e:
            if e.errno == 98:  # Address already in use
                logger.info(f"Port {port} is in use, trying next port...")
                port += 1
                if port > 65535: # 端口号范围检查
                    logger.error("No available ports found in range 8000-65535")
                    sys.exit(1)
                try:
                    kill_process_on_port(port)
                except Exception as kill_error:
                    logger.error(f"Failed to kill process on port {port}: {kill_error}")
            else:
                logger.error(f"Failed to start server: {e}")
                raise # 重新抛出非端口占用的错误

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Start a simple HTTP file sharing server.')
    parser.add_argument('shared_directory', nargs='?', default=None, help='The directory to share. Defaults to Desktop if not provided.')
    parser.add_argument('--logs', action='store_true', help='Enable logging to a file.')
    parser.add_argument('--port', type=int, default=8000, help='Initial port to listen on (default: 8000).')
    args = parser.parse_args()

    start_serve(args.shared_directory, args.logs, args.port)
