#!/usr/bin/env python3

import os
from pathlib import Path
from typing import List, Optional
from tqdm import tqdm

class FileTreeGenerator:
    def __init__(self,
                 dir_path: str,
                 output_file: str,
                 ignore_dirs: Optional[List[str]] = None,
                 ignore_files: Optional[List[str]] = None,
                 include_extensions: Optional[List[str]] = None,
                 exclude_extensions: Optional[List[str]] = None,
                 max_file_size: int = 10 * 1024 * 1024):  # 默认最大10MB
        """
        初始化文件树生成器
        :param dir_path: 源目录路径
        :param output_file: 输出文件路径
        :param ignore_dirs: 要忽略的目录列表
        :param ignore_files: 要忽略的文件列表
        :param include_extensions: 只包含的文件扩展名列表
        :param exclude_extensions: 要排除的文件扩展名列表
        :param max_file_size: 处理的最大文件大小(字节)
        """
        self.dir_path = Path(dir_path).resolve()
        self.output_file = Path(output_file).resolve()
        self.ignore_dirs = set(ignore_dirs or [])
        self.ignore_files = set(ignore_files or [])
        self.include_extensions = set(ext.lower() for ext in (include_extensions or []))
        self.exclude_extensions = set(ext.lower() for ext in (exclude_extensions or []))
        self.max_file_size = max_file_size

    def should_include_file(self, file_path: Path) -> bool:
        """判断是否应该包含该文件"""
        try:
            # 检查文件大小
            if file_path.stat().st_size > self.max_file_size:
                return False

            if file_path.name in self.ignore_files or file_path.name.startswith('.'):
                return False

            ext = file_path.suffix.lower().lstrip('.')

            if self.include_extensions and ext not in self.include_extensions:
                return False

            if ext in self.exclude_extensions:
                return False

            return True
        except Exception:
            return False

    def should_include_dir(self, dirname: str) -> bool:
        """判断是否应该包含该目录"""
        return not (dirname in self.ignore_dirs or dirname.startswith('.'))

    def generate(self) -> bool:
        """生成目录树和文件内容"""
        try:
            self.output_file.parent.mkdir(parents=True, exist_ok=True)

            with open(self.output_file, 'w', encoding='utf-8') as f:
                # 写入配置信息
                f.write("## Configuration\n")
                config_items = {
                    "Ignored directories": self.ignore_dirs,
                    "Ignored files": self.ignore_files,
                    "Included extensions": self.include_extensions,
                    "Excluded extensions": self.exclude_extensions,
                    "Max file size": f"{self.max_file_size/1024/1024:.2f}MB"
                }
                for key, value in config_items.items():
                    if value:
                        f.write(f"{key}: {value}\n")
                f.write("\n")

                # 收集所有文件和目录
                all_items = []
                print("Scanning directory structure...")
                for root, dirs, files in os.walk(self.dir_path):
                    root_path = Path(root)
                    dirs[:] = [d for d in dirs if self.should_include_dir(d)]
                    
                    rel_path = root_path.relative_to(self.dir_path)
                    level = len(rel_path.parts)
                    
                    all_items.append(('dir', root_path, level))
                    for file in files:
                        file_path = root_path / file
                        if self.should_include_file(file_path):
                            all_items.append(('file', file_path, level + 1))

                # 写入目录结构
                f.write("## Directory Structure\n")
                for item_type, path, level in tqdm(all_items, desc="Writing directory structure"):
                    indent = "    " * level
                    if item_type == 'dir':
                        f.write(f"{indent}{path.name}/\n")
                    else:
                        f.write(f"{indent}{path.name}\n")

                # 写入文件内容
                f.write("\n## File Contents\n")
                files_to_process = [item for item in all_items if item[0] == 'file']
                
                for _, file_path, _ in tqdm(files_to_process, desc="Processing files"):
                    relative_path = file_path.relative_to(self.dir_path)
                    f.write(f"\n### File: {relative_path}\n")
                    f.write("```\n")
                    try:
                        with open(file_path, 'r', encoding='utf-8') as content_file:
                            content = content_file.read().rstrip()
                            f.write(content)
                            f.write("\n")
                    except UnicodeDecodeError:
                        f.write("[Binary file or encoding error]\n")
                    except Exception as e:
                        f.write(f"[Error reading file: {str(e)}]\n")
                    f.write("```\n")
                    f.write("\n---\n")

            return True

        except Exception as e:
            print(f"Error generating file tree: {str(e)}")
            return False

def main(dir_path,output_file):

    # 配置示例
    config = {
        'dir_path': dir_path,
        'output_file': output_file,
        'ignore_dirs': [
            'node_modules',
            'dist',
            'build',
            '.git',
            '__pycache__'
        ],
        'ignore_files': [
            'package-lock.json',
            '.DS_Store'
        ],
        'include_extensions': [
            'py',
            'txt',
            'md',
            'json',
            'html',
            'css',
            'js'
        ],
        'exclude_extensions': [
            'exe',
            'dll',
            'so',
            'dylib'
        ],
        'max_file_size': 10 * 1024 * 1024  # 10MB
    }

    # 检查源目录是否存在
    if not os.path.exists(dir_path):
        print(f"错误: 目录 {dir_path} 不存在!")
        return

    # 创建生成器并执行
    generator = FileTreeGenerator(**config)
    if generator.generate():
        print(f"成功: 目录结构和文件内容已保存到 {output_file}")
    else:
        print("错误: 生成文件失败!")


if __name__ == "__main__":
    try:

        # 正确方式：移除逗号，确保是字符串类型
        # dir_path = str(Path.home() / "Desktop/Magic-HTML-API")
        dir_path = "/Users/sanbo/Desktop/hf"
        output_file = str(Path.home() / "Desktop/file_tree.txt")
        
        # 添加路径检查
        if not os.path.exists(dir_path):
            print(f"错误: 目录 {dir_path} 不存在!")
            exit(1)
            
        main(dir_path, output_file)
        
    except Exception as e:
        print(f"发生错误: {str(e)}")

