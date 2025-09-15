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
import mimetypes
from pathlib import Path
from functools import lru_cache

# --- 条件导入 markdown ---
try:
    import markdown
    MARKDOWN_AVAILABLE = True
    # logging might not be set up yet, but if it is, this will log
    if 'logger' in globals():
        logger.info("Markdown library found. Markdown rendering enabled.")
    else:
        print("Markdown library found. Markdown rendering enabled.")
except ImportError:
    MARKDOWN_AVAILABLE = False
    # Ensure logger is available
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logger = logging.getLogger(__name__)
    logger.warning("Markdown library not found. Install with 'pip install markdown' for Markdown rendering support.")

# --- Configuration ---
PAGE_SIZE = 20
DIRECTORY_SIZE_CACHE_TTL = 300
STATIC_DIR_NAME = "static"
# 是否启用语法高亮 (设为 False 可显著提升大文件加载速度和降低资源消耗)
ENABLE_SYNTAX_HIGHLIGHTING = True
# 分块读取文件的大小 (字节)
FILE_CHUNK_SIZE = 16 * 1024 # 16KB chunks

# --- Setup Logging (Ensure it's available early) ---
if 'logger' not in globals() or not logger.hasHandlers():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logger = logging.getLogger(__name__)
else:
    # Logger was already set up by markdown import block
    pass

# --- Utility Functions ---

def get_local_ip() -> str:
    """获取本机IP地址"""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        local_ip = s.getsockname()[0]
    except Exception as e:
        logger.error(f"Failed to get local IP address: {e}")
        local_ip = '127.0.0.1'
    finally:
        if s is not None:
            s.close()
    return local_ip

def kill_process_on_port(port: int) -> None:
    """尝试终止占用指定端口的进程"""
    try:
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                for conn in proc.connections(kind='inet'):
                    if conn.laddr.port == port and proc.info['pid'] > 1:
                        logger.info(f"Terminating process {proc.info['name']} (PID: {proc.info['pid']}) on port {port}")
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except psutil.TimeoutExpired:
                            logger.warning(f"Process {proc.info['pid']} did not terminate gracefully, killing it.")
                            proc.kill()
                        logger.info(f"Process on port {port} terminated.")
                        return
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception as e:
        logger.error(f"Error while trying to kill process on port {port}: {e}")

def get_content_type(path: str) -> str:
    """根据文件扩展名猜测 Content-Type"""
    content_type, _ = mimetypes.guess_type(path)
    return content_type or 'application/octet-stream'

# --- HTML Templates ---

# Main directory listing template
DIRECTORY_LIST_TEMPLATE = '''<!DOCTYPE HTML>
<html>
<head>
    <meta http-equiv="Content-Type" content="text/html; charset={charset}">
    <title>{title}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        :root {{ --primary-color: #007BFF; --hover-color: #0056b3; --header-bg: #343a40; --item-bg: #ffffff; --text-color: #333; --secondary-text: #6c757d; --action-color: #28a745; --action-hover: #1e7e34; --border-color: #dee2e6; --shadow: rgba(0,0,0,0.1); }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background-color: #f8f9fa; margin: 0; padding: 20px; }}
        h1 {{ font-size: 1.5rem; text-align: center; color: var(--text-color); margin-bottom: 1.5rem; }}
        .search {{ text-align: center; margin-bottom: 1.5rem; }}
        .search input[type="text"] {{ padding: 0.5rem; width: calc(100% - 220px); max-width: 300px; border: 1px solid #ccc; border-radius: 4px; }}
        .search input[type="submit"] {{ padding: 0.5rem 1rem; background-color: var(--primary-color); color: white; border: none; border-radius: 4px; cursor: pointer; margin-left: 5px; }}
        .search input[type="submit"]:hover {{ background-color: var(--hover-color); }}
        ul {{ list-style-type: none; padding: 0; width: 95%; max-width: 1200px; margin: 0 auto; }}
        li {{ display: flex; justify-content: space-between; align-items: center; background-color: var(--item-bg); padding: 0.75rem 1rem; margin-bottom: 0.5rem; border-radius: 6px; box-shadow: 0 1px 3px var(--shadow); transition: box-shadow 0.2s; }}
        li:hover {{ box-shadow: 0 2px 5px rgba(0,0,0,0.15); }}
        li.header {{ background-color: var(--header-bg); color: white; font-weight: bold; box-shadow: none; }}
        li.header span {{ color: white; }}
        a {{ text-decoration: none; color: var(--primary-color); font-weight: 500; }}
        a:hover {{ text-decoration: underline; }}
        .size, .date {{ color: var(--secondary-text); font-size: 0.875rem; }}
        .actions {{ color: var(--action-color); }}
        .actions a {{ color: var(--action-color); margin: 0 5px; font-size: 0.875rem; }}
        .actions a:hover {{ color: var(--action-hover); }}
        .pagination {{ text-align: center; margin: 2rem 0; }}
        .pagination a, .pagination span {{ display: inline-block; padding: 0.5rem 0.75rem; margin: 0 0.25rem; text-decoration: none; color: var(--primary-color); border: 1px solid var(--border-color); border-radius: 4px; }}
        .pagination a:hover {{ background-color: #e9ecef; }}
        .pagination .current {{ background-color: var(--primary-color); color: white; border-color: var(--primary-color); }}
        .pagination .disabled {{ color: var(--secondary-text); cursor: not-allowed; border-color: var(--border-color); }}
        .back-link {{ display: block; text-align: center; margin-top: 1.5rem; }}
        @media (max-width: 768px) {{
            ul {{ width: 100%; }}
            li {{ flex-direction: column; align-items: flex-start; }}
            .size, .date, .actions {{ width: 100%; margin-top: 0.25rem; }}
            .search input[type="text"] {{ width: 70%; margin-bottom: 0.5rem; }}
        }}
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
DIRECTORY_LIST_FOOTER = '''</ul>
<div class="pagination">{pagination_links}</div>
<div class="back-link"><a href="/">回到根目录</a></div>
</body></html>'''

# Code viewer template - 流式加载框架
CODE_VIEW_FRAME_TEMPLATE = '''<!DOCTYPE html>
<html>
<head>
    <title>Code Viewer - {filename}</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        :root {{ --bg-color: #f5f5f5; --header-bg: #222; --header-text: white; --code-bg: #fdfdfd; --loading-text: #888; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; margin: 0; padding: 0; background-color: var(--bg-color); color: #333; }}
        .header {{ background-color: var(--header-bg); color: var(--header-text); padding: 1rem 1.25rem; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #444; position: sticky; top: 0; z-index: 100; }}
        .header a {{ color: #66d9ef; text-decoration: none; font-weight: 500; }}
        .header a:hover {{ text-decoration: underline; }}
        #loading {{ text-align: center; padding: 2rem; color: var(--loading-text); font-size: 1rem; }}
        #code-container {{ padding: 1rem; }}
        pre {{ margin: 0; background-color: var(--code-bg); border-radius: 5px; padding: 1rem; overflow-x: auto; box-shadow: inset 0 0 5px rgba(0,0,0,0.05); }}
        code {{ font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace; font-size: 0.875rem; line-height: 1.4; display: block; white-space: pre-wrap; word-wrap: break-word; }}
        /* 滚动条样式 (可选) */
        ::-webkit-scrollbar {{ width: 10px; height: 10px; }}
        ::-webkit-scrollbar-track {{ background: #f1f1f1; border-radius: 5px; }}
        ::-webkit-scrollbar-thumb {{ background: #c1c1c1; border-radius: 5px; }}
        ::-webkit-scrollbar-thumb:hover {{ background: #a8a8a8; }}
        @media (prefers-color-scheme: dark) {{
            :root {{ --bg-color: #1e1e1e; --header-bg: #121212; --code-bg: #252526; --loading-text: #aaa; --header-text: #eee; }}
            body {{ background-color: var(--bg-color); color: #ccc; }}
            pre {{ background-color: var(--code-bg); box-shadow: inset 0 0 5px rgba(0,0,0,0.3); }}
            .header a {{ color: #4ec9b0; }}
            ::-webkit-scrollbar-track {{ background: #2a2a2a; }}
            ::-webkit-scrollbar-thumb {{ background: #555; }}
            ::-webkit-scrollbar-thumb:hover {{ background: #777; }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <h2>{filename}</h2>
        <a href="{raw_url}">下载原始文件</a>
    </div>
    <div id="loading">正在加载文件内容...</div>
    <div id="code-container">
        <pre><code id="code-content"></code></pre>
    </div>
    <script>
        const filePath = "{raw_url}";
        const contentUrl = filePath + "?action=get_content&chunk_size={chunk_size}";
        const codeElement = document.getElementById('code-content');
        const loadingElement = document.getElementById('loading');

        let decoder = new TextDecoder('utf-8');
        let initialRenderDone = false;

        async function loadFileContent() {{
            try {{
                const response = await fetch(contentUrl);
                if (!response.ok) {{
                    throw new Error(`HTTP error! status: ${{response.status}}`);
                }}
                const contentType = response.headers.get('content-type');
                if (!contentType || !contentType.startsWith('text/plain')) {{
                     throw new Error(`Invalid content type: ${{contentType}}`);
                }}

                const reader = response.body.getReader();
                loadingElement.style.display = 'none';
                let isFirstChunk = true;

                while (true) {{
                    const {{ done, value }} = await reader.read();
                    if (done) {{
                        console.log("Stream complete");
                        break;
                    }}
                    
                    const chunkText = decoder.decode(value, {{ stream: true }});
                    
                    if (isFirstChunk) {{
                        codeElement.textContent = chunkText;
                        if (window.Prism && {enable_highlight}) {{
                            try {{ Prism.highlightElement(codeElement); }} catch (e) {{ console.error("Prism error:", e); }}
                        }}
                        isFirstChunk = false;
                    }} else {{
                        codeElement.textContent += chunkText;
                    }}
                }}
                
                if (window.Prism && {enable_highlight}) {{
                    console.log("Performing final highlight...");
                    try {{
                        Prism.highlightAll();
                    }} catch (e) {{
                        console.error("Final Prism highlight error:", e);
                    }}
                }}
                
            }} catch (error) {{
                console.error('Error loading file content:', error);
                loadingElement.textContent = `加载失败: ${{error.message}}`;
                loadingElement.style.color = 'red';
            }}
        }}

        window.addEventListener('DOMContentLoaded', (event) => {{
            loadFileContent();
        }});
        
        if ({enable_highlight}) {{
            console.log("Loading Prism.js...");
            const link = document.createElement('link');
            link.rel = 'stylesheet';
            link.href = '/{static_dir}/prism.css';
            document.head.appendChild(link);
            
            const script = document.createElement('script');
            script.src = '/{static_dir}/prism.js';
            script.onload = function() {{
                console.log("Prism.js loaded.");
            }};
            document.head.appendChild(script);
        }}
    </script>
</body>
</html>'''

# Markdown viewer template
MARKDOWN_VIEWER_TEMPLATE = '''<!DOCTYPE html>
<html>
<head>
    <title>Markdown Viewer - {filename}</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        :root {{ --bg-color: #f8f9fa; --header-bg: #222; --header-text: white; --content-bg: #ffffff; --text-color: #212529; --border-color: #dee2e6; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; margin: 0; padding: 0; background-color: var(--bg-color); color: var(--text-color); }}
        .header {{ background-color: var(--header-bg); color: var(--header-text); padding: 1rem 1.25rem; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #444; position: sticky; top: 0; z-index: 100; }}
        .header a {{ color: #66d9ef; text-decoration: none; font-weight: 500; margin-left: 1rem; }}
        .header a:hover {{ text-decoration: underline; }}
        #loading {{ text-align: center; padding: 2rem; color: #6c757d; }}
        #md-container {{ padding: 2rem; background-color: var(--content-bg); margin: 1rem; border-radius: 5px; box-shadow: 0 0.125rem 0.25rem rgba(0,0,0,0.075); }}
        #md-content {{ line-height: 1.6; }}
        /* Basic Markdown Styles */
        #md-content h1, #md-content h2, #md-content h3 {{ margin-top: 1.5rem; margin-bottom: 1rem; }}
        #md-content p {{ margin-bottom: 1rem; }}
        #md-content pre {{ background-color: #f1f1f1; padding: 1rem; overflow-x: auto; border-radius: 4px; }}
        #md-content code {{ font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace; background-color: #f1f1f1; padding: 0.2em 0.4em; border-radius: 3px; }}
        #md-content pre code {{ background-color: transparent; padding: 0; }}
        #md-content blockquote {{ margin: 0 0 1rem; padding: 0.5rem 1rem; border-left: 0.25rem solid #dee2e6; }}
        #md-content ul, #md-content ol {{ margin-bottom: 1rem; padding-left: 2rem; }}
        #md-content li {{ margin-bottom: 0.25rem; }}
        #md-content table {{ border-collapse: collapse; width: 100%; margin-bottom: 1rem; }}
        #md-content th, #md-content td {{ padding: 0.75rem; vertical-align: top; border-top: 1px solid var(--border-color); }}
        #md-content th {{ background-color: #e9ecef; }}
        @media (prefers-color-scheme: dark) {{
            :root {{ --bg-color: #121212; --header-bg: #1e1e1e; --content-bg: #1e1e1e; --text-color: #e0e0e0; --border-color: #444; }}
            body {{ background-color: var(--bg-color); color: var(--text-color); }}
            #md-container {{ background-color: var(--content-bg); box-shadow: 0 0.125rem 0.25rem rgba(0,0,0,0.2); }}
            #md-content pre, #md-content code, #md-content blockquote {{ background-color: #2a2a2a; }}
            #md-content th {{ background-color: #3a3a3a; }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <h2>{filename}</h2>
        <div>
            <a href="{raw_url}" target="_blank">查看源码</a>
            <a href="{raw_url}">下载原始文件</a>
        </div>
    </div>
    <div id="loading">正在渲染 Markdown...</div>
    <div id="md-container">
        <div id="md-content"></div>
    </div>
    <script>
        const contentUrl = "{raw_url}?action=get_content&render=html";

        async function loadMarkdownContent() {{
            const loadingElement = document.getElementById('loading');
            const contentElement = document.getElementById('md-content');
            try {{
                const response = await fetch(contentUrl);
                if (!response.ok) {{
                    throw new Error(`HTTP error! status: ${{response.status}}`);
                }}
                const htmlText = await response.text();
                loadingElement.style.display = 'none';
                contentElement.innerHTML = htmlText;
            }} catch (error) {{
                console.error('Error loading Markdown content:', error);
                loadingElement.textContent = `渲染失败: ${{error.message}}`;
                loadingElement.style.color = 'red';
            }}
        }}

        window.addEventListener('DOMContentLoaded', (event) => {{
            loadMarkdownContent();
        }});
    </script>
</body>
</html>'''

# --- Main Handler Class ---

class CustomHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    zip_creation_lock = threading.Lock()
    
    @lru_cache(maxsize=128)
    def _cached_get_directory_size(self, path: str, mtime: float) -> int:
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
        try:
            mtime = Path(path).stat().st_mtime
        except OSError as e:
            logger.error(f"Error getting mtime for {path}: {e}")
            return 0
        cache_key_path = str(Path(path).resolve())
        return self._cached_get_directory_size(cache_key_path, mtime)

    def list_directory(self, path: str):
        try:
            path_obj = Path(path)
            entries = [p.name for p in path_obj.iterdir()]
        except OSError as e:
            logger.error(f"Failed to list directory {path}: {e}")
            self.send_error(http.HTTPStatus.NOT_FOUND, "No permission to list directory")
            return None

        query_components = parse_qs(self.path.split('?')[-1]) if '?' in self.path else {}
        page = int(query_components.get('page', [1])[0])
        search = query_components.get('search', [''])[0].lower()

        if search:
            entries = [entry for entry in entries if search in entry.lower()]

        entries.sort(key=lambda a: a.lower())
        total_entries = len(entries)
        total_pages = max(1, (total_entries + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * PAGE_SIZE
        end_idx = start_idx + PAGE_SIZE
        entries = entries[start_idx:end_idx]

        r = self.generate_html_list(str(path_obj), entries, page, total_pages, search)
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
        try:
            displaypath = unquote(self.path, errors='surrogatepass')
        except UnicodeDecodeError:
            displaypath = unquote(self.path.encode('utf-8', 'surrogateescape').decode('utf-8', 'replace'))
        displaypath = escape(displaypath, quote=False)
        enc = sys.getfilesystemencoding()
        title = '文件共享目录索引'

        html_parts = [DIRECTORY_LIST_TEMPLATE.format(
            charset=enc,
            title=escape(title),
            escaped_search=escape(search)
        )]

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
                    size = stat_result.st_size
                    size_str = self.human_readable_size(size)
                except OSError as e:
                    logger.warning(f"Could not get size for {fullname}: {e}")
                    size_str = "N/A"
                html_parts.append(self.generate_file_entry(linkname, displayname, size_str, mod_time_str))

        pagination_links = []
        if page > 1:
            pagination_links.append(f'<a href="?page={page-1}&search={quote(search)}">&laquo; 上一页</a>')
        else:
            pagination_links.append('<span class="disabled">&laquo; 上一页</span>')

        start_page = max(1, page - 2)
        end_page = min(total_pages, page + 2)
        if start_page > 1:
            pagination_links.append(f'<a href="?page=1&search={quote(search)}">1</a>')
            if start_page > 2:
                pagination_links.append('<span>...</span>')
        for p in range(start_page, end_page + 1):
            if p == page:
                pagination_links.append(f'<span class="current">{p}</span>')
            else:
                pagination_links.append(f'<a href="?page={p}&search={quote(search)}">{p}</a>')
        if end_page < total_pages:
            if end_page < total_pages - 1:
                pagination_links.append('<span>...</span>')
            pagination_links.append(f'<a href="?page={total_pages}&search={quote(search)}">{total_pages}</a>')

        if page < total_pages:
            pagination_links.append(f'<a href="?page={page+1}&search={quote(search)}">下一页 &raquo;</a>')
        else:
            pagination_links.append('<span class="disabled">下一页 &raquo;</span>')

        html_parts.append(DIRECTORY_LIST_FOOTER.format(pagination_links=''.join(pagination_links)))
        return ''.join(html_parts)

    def generate_directory_entry(self, linkname: str, displayname: str, size_str: str, mod_time_str: str) -> str:
        safe_linkname = escape(linkname, quote=True)
        safe_displayname = escape(displayname, quote=False)
        return (f'<li><a href="{safe_linkname}/">{safe_displayname}/</a>'
                f'<span class="size">{size_str}</span>'
                f'<span class="date">{mod_time_str}</span>'
                f'<span class="actions"><a href="{safe_linkname}.zip">下载 (ZIP)</a></span></li>')

    def generate_file_entry(self, linkname: str, displayname: str, size_str: str, mod_time_str: str) -> str:
        safe_linkname = escape(linkname, quote=True)
        safe_displayname = escape(displayname, quote=False)
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
        if size < 0:
            return "N/A"
        for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
            if size < 1024.0:
                return f"{size:.{decimal_places}f} {unit}"
            size /= 1024.0
        return f"{size:.{decimal_places}f} EB"

    def create_zip(self, directory: str, zip_name: str) -> bool:
        with self.zip_creation_lock:
            if os.path.exists(zip_name):
                logger.info(f"ZIP file {zip_name} already exists.")
                return True
            try:
                logger.info(f"Creating ZIP file {zip_name} for directory {directory}")
                directory_path = Path(directory)
                zip_path = Path(zip_name)
                temp_zip_path = zip_path.with_suffix(zip_path.suffix + f".tmp_{os.getpid()}")
                with ZipFile(temp_zip_path, 'w') as zipf:
                    for file_path in directory_path.rglob('*'):
                        if file_path.is_file():
                            arcname = file_path.relative_to(directory_path)
                            zipf.write(file_path, arcname)
                shutil.move(str(temp_zip_path), str(zip_path))
                logger.info(f"Successfully created ZIP file {zip_name}")
                return True
            except Exception as e:
                logger.error(f"Failed to create ZIP file {zip_name}: {e}")
                try:
                    if 'temp_zip_path' in locals() and temp_zip_path.exists():
                        temp_zip_path.unlink()
                except Exception as cleanup_error:
                    logger.error(f"Failed to clean up temp ZIP file {temp_zip_path}: {cleanup_error}")
                return False

    def handle_text_file(self, file_path_str: str, mime_type: str):
        """处理文本文件的预览，支持代码高亮 (流式加载框架)。"""
        file_path = Path(file_path_str)
        logger.debug(f"Inside handle_text_file for: {file_path}")
        
        # 检查是否是获取内容的 AJAX 请求 (通过查询参数 action=get_content)
        query_components = parse_qs(self.path.split('?')[-1]) if '?' in self.path else {}
        if query_components.get('action', [None])[0] == 'get_content':
            # 检查是否请求 HTML 渲染 (用于 Markdown)
            render_html = query_components.get('render', [None])[0] == 'html'
            if render_html and MARKDOWN_AVAILABLE and file_path.suffix.lower() == '.md':
                 logger.debug(f"Routing to _serve_file_content_as_html for: {file_path}")
                 self._serve_file_content_as_html(file_path) # 处理 Markdown 渲染
                 return
            logger.debug(f"Routing to _serve_file_content for: {file_path}")
            self._serve_file_content(file_path)
            return

        # --- 否则，返回代码查看器框架 ---
        logger.debug(f"Generating viewer frame for: {file_path}")
        try:
            file_extension = file_path.suffix.lower()
            # 生成原始文件下载链接 (self.path 是不带 action 参数的原始请求路径)
            raw_url = self.path.split('?')[0] 
            logger.debug(f"Raw URL for links: {raw_url}")

            # 为 Markdown 文件提供 HTML 渲染查看器
            if file_extension == '.md' and MARKDOWN_AVAILABLE:
                logger.debug(f"Generating Markdown viewer frame for: {file_path}")
                html_content = self._generate_markdown_viewer_html(file_path.name, raw_url)
            else:
                # 为其他文本文件提供代码查看器
                logger.debug(f"Generating Code viewer frame for: {file_path}")
                html_content = CODE_VIEW_FRAME_TEMPLATE.format(
                    filename=escape(file_path.name, quote=False),
                    raw_url=raw_url,
                    static_dir=STATIC_DIR_NAME,
                    enable_highlight=str(ENABLE_SYNTAX_HIGHLIGHTING).lower(),
                    chunk_size=FILE_CHUNK_SIZE
                )

            self.send_response(http.HTTPStatus.OK)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_content.encode('utf-8'))

        except Exception as e:
            logger.error(f"Unexpected error generating code view frame for {file_path}: {e}", exc_info=True)
            self.send_error(http.HTTPStatus.INTERNAL_SERVER_ERROR, "Error generating code view")

    def _serve_file_content(self, file_path: Path):
        """流式传输文件内容（纯文本）给前端 AJAX 请求。"""
        logger.debug(f"Serving file content (streaming text) for: {file_path}")
        try:
            if not file_path.is_file():
                 self.send_error(http.HTTPStatus.NOT_FOUND, "File not found")
                 return

            self.send_response(http.HTTPStatus.OK)
            # 关键：强制使用 text/plain，确保浏览器显示而非下载
            self.send_header("Content-type", "text/plain")
            self.send_header("Content-Encoding", "identity")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            with open(file_path, 'rb') as f:
                while True:
                    chunk = f.read(FILE_CHUNK_SIZE)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush() 
                    
            logger.debug(f"Finished streaming text content for: {file_path}")

        except IOError as e:
            logger.error(f"IOError streaming text content for {file_path}: {e}")
            try:
                self.send_error(http.HTTPStatus.NOT_FOUND, "File not found or read error")
            except:
                pass
        except BrokenPipeError:
            logger.warning(f"Broken pipe error while streaming text content for {file_path}. Client likely disconnected.")
        except Exception as e:
            logger.error(f"Unexpected error streaming text content for {file_path}: {e}", exc_info=True)
            try:
                self.send_error(http.HTTPStatus.INTERNAL_SERVER_ERROR, "Error reading file")
            except:
                pass

    def _serve_file_content_as_html(self, file_path: Path):
        """将 Markdown 文件内容转换为 HTML 并发送。"""
        if not MARKDOWN_AVAILABLE:
            self.send_error(http.HTTPStatus.INTERNAL_SERVER_ERROR, "Markdown rendering not available. Please install the 'markdown' package.")
            return

        logger.debug(f"Serving file content as HTML (Markdown render) for: {file_path}")
        try:
            if not file_path.is_file():
                self.send_error(http.HTTPStatus.NOT_FOUND, "File not found")
                return

            with open(file_path, 'r', encoding='utf-8') as f:
                md_content = f.read()

            # 转换为 HTML
            html_content = markdown.markdown(md_content, extensions=['fenced_code', 'tables', 'toc'])

            self.send_response(http.HTTPStatus.OK)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            
            self.wfile.write(html_content.encode('utf-8'))
            logger.debug(f"Finished sending HTML content for: {file_path}")

        except UnicodeDecodeError as e:
            logger.error(f"UnicodeDecodeError reading Markdown file {file_path}: {e}")
            self.send_error(http.HTTPStatus.BAD_REQUEST, "File encoding error")
        except Exception as e:
            logger.error(f"Error rendering Markdown file {file_path}: {e}", exc_info=True)
            self.send_error(http.HTTPStatus.INTERNAL_SERVER_ERROR, "Error rendering Markdown")

    def _generate_markdown_viewer_html(self, filename: str, raw_url: str) -> str:
        """生成 Markdown 文件的 HTML 查看器页面。"""
        return MARKDOWN_VIEWER_TEMPLATE.format(
            filename=escape(filename, quote=False),
            raw_url=escape(raw_url, quote=True)
        )

    def do_GET(self):
        logger.info(f"GET request for: {self.path}")
        
        if self.path.startswith(f'/{STATIC_DIR_NAME}/'):
            logger.debug(f"Serving static file: {self.path}")
            super().do_GET()
            return

        full_path = Path(self.translate_path(self.path))
        logger.debug(f"Mapped path: {self.path} -> {full_path}")

        if self.path.endswith(".zip") and full_path.is_file():
            logger.debug(f"Serving existing ZIP file: {full_path}")
            try:
                fs = full_path.stat()
                with open(full_path, 'rb') as f:
                    self.send_response(http.HTTPStatus.OK)
                    self.send_header("Content-type", "application/zip")
                    self.send_header("Content-Length", str(fs.st_size))
                    self.end_headers()
                    shutil.copyfileobj(f, self.wfile)
                return
            except IOError as e:
                logger.error(f"IOError serving existing ZIP {full_path}: {e}")
                self.send_error(http.HTTPStatus.NOT_FOUND, "File not found")
                return
            except BrokenPipeError:
                logger.warning("Broken pipe error while sending existing ZIP file")
                return

        if self.path.endswith(".zip"):
            directory_rel_path = self.path[:-4]
            directory_full_path = Path(self.translate_path(directory_rel_path))
            if directory_full_path.is_dir():
                zip_full_path = full_path
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
                            # try: zip_full_path.unlink()
                            # except: pass
                            pass
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

        # --- 关键修改：在这里增加详细的日志和更宽松的判断 ---
        mime_type = get_content_type(str(full_path))
        logger.debug(f"File: {full_path}, Guessed MIME type: {mime_type}")
        
        # 定义明确需要预览的文本文件扩展名
        previewable_extensions = {
            '.txt', '.csv', '.log', '.md', '.json', '.xml',
            '.yaml', '.yml', '.ini', '.cfg',
            '.py', '.js', '.html', '.css', '.java', '.c', '.cpp', '.h', '.cs',
            '.rb', '.php', '.go', '.swift', '.ts', '.sh', '.bash', '.sql'
        }
        
        # 判断是否为文本类文件：标准文本类型 OR 已知扩展名
        is_text_like = (
            mime_type.startswith('text/') or 
            mime_type in ['application/json', 'application/xml'] or
            full_path.suffix.lower() in previewable_extensions
        )
        
        if is_text_like and full_path.is_file():
             logger.debug(f"Handling as text file for preview: {full_path}")
             self.handle_text_file(str(full_path), mime_type)
             return
        else:
             logger.debug(f"Delegating to parent class (or not text-like): {full_path}, MIME: {mime_type}, Exists: {full_path.is_file()}")
        # --- 关键修改结束 ---

        logger.debug(f"Delegating to parent class for: {full_path}")
        super().do_GET()


def start_serve(shared_directory: str = None, save_logs: bool = False, initial_port: int = 8000):
    if not shared_directory:
        if os.name == 'nt':
            shared_directory = os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop')
        else:
            shared_directory = os.path.join(os.path.expanduser('~'), 'Desktop')
    
    shared_path = Path(shared_directory)
    if not shared_path.exists():
        logger.error(f"Shared directory does not exist: {shared_directory}")
        sys.exit(1)
    if not shared_path.is_dir():
        logger.error(f"Shared path is not a directory: {shared_directory}")
        sys.exit(1)

    if save_logs:
        log_filename = f'server_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
        file_handler = logging.FileHandler(log_filename)
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        logger.addHandler(file_handler)
        logger.info(f"Logging to file: {log_filename}")

    static_path = shared_path / STATIC_DIR_NAME
    if not static_path.exists() or not static_path.is_dir():
        logger.warning(f"Static directory '{STATIC_DIR_NAME}' not found. Syntax highlighting will be disabled or may not work.")
        global ENABLE_SYNTAX_HIGHLIGHTING
        ENABLE_SYNTAX_HIGHLIGHTING = False

    os.chdir(str(shared_path))
    ip = get_local_ip()
    port = initial_port

    try:
        kill_process_on_port(port)
    except Exception as e:
        logger.warning(f"Could not kill process on initial port {port}: {e}. Trying next port.")

    Handler = CustomHTTPRequestHandler

    while True:
        try:
            with socketserver.TCPServer(("", port), Handler) as httpd:
                logger.info(f"Starting server at http://{ip}:{port}")
                print(f"Serving at http://{ip}:{port}")
                httpd.serve_forever()
                break
        except OSError as e:
            if e.errno == 98:
                logger.info(f"Port {port} is in use, trying next port...")
                port += 1
                if port > 65535:
                    logger.error("No available ports found in range 8000-65535")
                    sys.exit(1)
                try:
                    kill_process_on_port(port)
                except Exception as kill_error:
                    logger.error(f"Failed to kill process on port {port}: {kill_error}")
            else:
                logger.error(f"Failed to start server: {e}")
                raise

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Start a simple HTTP file sharing server.')
    parser.add_argument('shared_directory', nargs='?', default=None, help='The directory to share. Defaults to Desktop if not provided.')
    parser.add_argument('--logs', action='store_true', help='Enable logging to a file.')
    parser.add_argument('--port', type=int, default=8000, help='Initial port to listen on (default: 8000).')
    args = parser.parse_args()

    start_serve(args.shared_directory, args.logs, args.port)
