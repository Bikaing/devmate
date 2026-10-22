"""只读、限额的代码工具；历史审查只读取对应 Git 版本。"""
from pathlib import Path, PurePosixPath
import subprocess

EXTENSIONS = {'.py', '.js', '.ts', '.tsx', '.jsx', '.java', '.go', '.md', '.json',
              '.toml', '.yaml', '.yml', '.sql', '.html', '.css', '.txt', '.sh', '.ps1'}


class RepositoryReader:
    def __init__(self, root, mode, head_ref):
        self.root = Path(root).resolve()
        self.mode, self.head_ref = mode, head_ref
        self.notes, self.cache, self.read_bytes = [], {}, 0
        args = ['ls-files', '-z', '--cached', '--others', '--exclude-standard'] if mode == 'uncommitted' else ['ls-tree', '-rz', '--name-only', head_ref]
        paths = list(dict.fromkeys(p for p in self.git(*args).decode('utf-8', 'replace').split('\0') if p))
        safe = [p for p in paths if self.allowed(p)]
        self.paths = safe[:2000]
        if len(safe) > 2000:
            self.notes.append('仅扫描前 2000 个可读代码文件')

    def git(self, *args):
        result = subprocess.run(['git', '-c', 'core.quotePath=false', *args], cwd=self.root,
                                capture_output=True, timeout=15)
        if result.returncode:
            raise ValueError('无法读取审查版本的仓库文件')
        return result.stdout

    def allowed(self, name):
        p = PurePosixPath(name)
        return (bool(name) and not p.is_absolute() and '\\' not in name and ':' not in name
                and '..' not in p.parts and p.suffix.lower() in EXTENSIONS
                and not any(x.startswith('.') or x.lower() in
                            {'node_modules', 'models', 'venv', 'dist', 'build', '__pycache__'} for x in p.parts)
                and not any(x in p.name.lower() for x in ('secret', 'credential', 'private_key')))

    def content(self, name):
        if name in self.cache:
            return self.cache[name]
        if name not in self.paths or not self.allowed(name):
            raise ValueError('路径不在允许读取的仓库代码列表内')
        if self.read_bytes >= 2_000_000:
            raise ValueError('代码读取已达到 2MB 限额')
        if self.mode == 'uncommitted':
            path = self.root / name
            if path.is_symlink() or self.root not in path.resolve().parents:
                raise ValueError('拒绝仓库外路径或符号链接')
            if any(p.is_symlink() for p in path.parents if p != self.root and self.root in p.parents):
                raise ValueError('拒绝链接目录')
            with path.open('rb') as stream:
                data = stream.read(80001)
        else:
            kind = self.git('ls-tree', self.head_ref, '--', name)
            if not kind.startswith((b'100644 ', b'100755 ')):
                raise ValueError('拒绝非普通 Git 文件')
            if int(self.git('cat-file', '-s', f'{self.head_ref}:{name}')) > 80000:
                raise ValueError('文件超过读取上限')
            data = self.git('show', f'{self.head_ref}:{name}')
        if len(data) > 80000 or b'\x00' in data:
            raise ValueError('文件过大或不是文本')
        self.read_bytes += len(data)
        value = data.decode('utf-8', 'replace')
        self.cache[name] = value
        return value

    def read(self, name, start_line=1):
        if not 1 <= start_line <= 100000:
            raise ValueError('无效行号')
        lines = self.content(name).splitlines()
        if not lines or start_line > len(lines):
            raise ValueError('请求行号超出文件范围')
        selected, chars = [], 0
        for line in lines[start_line - 1:start_line + 59]:
            if chars + len(line) > 12000:
                break
            selected.append(line)
            chars += len(line) + 1
        if not selected:
            raise ValueError('单行超过读取上限')
        return dict(path=name, start_line=start_line, end_line=start_line + len(selected) - 1,
                    content='\n'.join(selected))

    def search(self, term):
        if not 2 <= len(term) <= 100 or '\n' in term:
            raise ValueError('搜索词必须为 2–100 字符的单行字面文本')
        results = []
        for name in self.paths:
            try:
                content = self.content(name)
                for number, line in enumerate(content.splitlines(), 1):
                    if term in line:
                        results.append(self.read(name, max(1, number - 8)))
                        break
            except (OSError, ValueError):
                continue
            if len(results) >= 8:
                self.notes.append(f'搜索 {term!r} 最多返回 8 个文件')
                break
        if self.read_bytes >= 2_000_000:
            self.notes.append('搜索达到 2MB 扫描限额，可能仍有其他引用')
        return results
