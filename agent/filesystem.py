"""Text-file operations through no-follow directory descriptors."""
from __future__ import annotations
from contextlib import contextmanager
import fnmatch
import errno
import hashlib
import os
from pathlib import Path, PurePath
import stat
import threading
import uuid
from mcp.server.mcpserver.exceptions import ToolError
from images import ImageReader

BLOCKED = {'.ssh', '.aws', '.gnupg', '.config', '.codex', '.claude'}
SKIP_SEARCH = BLOCKED | {'.git', '.venv', 'node_modules', '__pycache__'}


def blocked(name: str) -> bool:
    return name in BLOCKED or name == '.env' or name.startswith('.env.')


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Files:
    def __init__(self, settings, storage):
        self.roots = {key: Path(value) for key, value in settings['workspaces'].items()}
        self.maximum = int(settings['max_file_bytes'])
        self.storage = storage
        self.full_machine = settings.get('permission_mode') == 'full_machine'
        self.protected = [Path(p) for p in settings.get('protected_paths', [])]
        self.lock = threading.RLock()
        self.images = ImageReader(self)

    def allowed(self, path):
        if self.full_machine and any(path == p or path.is_relative_to(p) for p in self.protected):
            raise ToolError('LocalPilot 自身的密钥、程序和验收数据库由本机管理接口保护；其余目录按 macOS 账号权限访问。')

    def io_error(self, exc):
        if exc.errno == errno.ENOENT:
            return ToolError('文件或目录不存在。')
        if exc.errno in (errno.EACCES, errno.EPERM):
            return ToolError('当前 macOS 账号或系统隐私设置拒绝访问此路径。')
        if exc.errno in (errno.ENOTDIR, errno.ELOOP):
            return ToolError('路径中有非目录项目或读取时路径发生变化。' if self.full_machine else '目录不可访问；目录必须存在，且不能经过符号链接。')
        return ToolError(f'文件系统操作失败：{exc.strerror or type(exc).__name__}。')

    def path(self, workspace: str, path: str = '.') -> tuple[Path, tuple[str, ...]]:
        if workspace not in self.roots:
            raise ToolError('未知 workspace；先调用 device_status 查看已授权目录。')
        root = self.roots[workspace]
        if self.full_machine:
            if '\x00' in path:
                raise ToolError('路径不能包含空字符。')
            value = Path(path).expanduser()
            value = (value if value.is_absolute() else root / value).resolve()
            self.allowed(value)
            return Path('/'), value.relative_to('/').parts
        value = PurePath(path)
        if '\x00' in path or '..' in value.parts:
            raise ToolError('路径不能包含空字符或 ..。')
        if value.is_absolute():
            try:
                value = value.relative_to(root)
            except ValueError:
                raise ToolError('路径位于已授权 workspace 之外。') from None
        if any(blocked(part) for part in value.parts):
            raise ToolError('该路径属于本机保留的凭据或配置目录。')
        return root, value.parts

    @contextmanager
    def directory(self, root: Path, parts: tuple[str, ...], create: bool = False):
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd
        except OSError as exc:
            raise self.io_error(exc) from exc
        finally:
            os.close(fd)

    def read_bytes(self, fd: int, name: str) -> tuple[bytes, int]:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(descriptor, 'rb') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or (info.st_nlink != 1 and not self.full_machine):
                raise ToolError('仅支持普通文件；不读取目录或设备。' if self.full_machine else '仅支持普通文件；不读取设备、符号链接或硬链接。')
            if info.st_size > self.maximum:
                raise ToolError(f'文件超过 {self.maximum} 字节上限。')
            data = file.read(self.maximum + 1)
            if len(data) > self.maximum:
                raise ToolError('文件超过大小上限。')
            return data, stat.S_IMODE(info.st_mode)

    def read(self, workspace, path, offset=0, max_chars=24000):
        return self._read(workspace, path, offset, max_chars, record=True)

    def read_image(self, workspace, path, max_side=1600, crop=None, frame_index=0):
        return self.images.read(workspace, path, max_side, crop, frame_index)

    def fingerprint(self, workspace, path, record=True):
        """Hash actual file bytes, including images, without decoding text."""
        root, parts = self.path(workspace, path)
        if not parts:
            raise ToolError('请指定文件路径。')
        try:
            with self.directory(root, parts[:-1]) as fd:
                data, _ = self.read_bytes(fd, parts[-1])
        except OSError as exc:
            raise self.io_error(exc) from exc
        result = {'workspace': workspace, 'path': str(root.joinpath(*parts)), 'sha256': digest(data), 'bytes': len(data)}
        result['receipt_id'] = self.storage.event('file_sha256', result) if record else None
        return result

    def peek(self, workspace, path, offset=0, max_chars=24000):
        """Same as read, but for internal status polling: no audit receipt is written."""
        return self._read(workspace, path, offset, max_chars, record=False)

    def _read(self, workspace, path, offset, max_chars, record):
        if offset < 0 or not 1 <= max_chars <= 64000:
            raise ToolError('offset 必须非负，max_chars 必须在 1–64000 之间。')
        root, parts = self.path(workspace, path)
        if not parts:
            raise ToolError('请指定文件路径。')
        try:
            with self.directory(root, parts[:-1]) as fd:
                data, _ = self.read_bytes(fd, parts[-1])
            text = data.decode('utf-8')
        except OSError as exc:
            raise self.io_error(exc) from exc
        except UnicodeError as exc:
            raise ToolError('文件不是有效的 UTF-8 文本。图片请使用 read_image。') from exc
        result = {'workspace': workspace, 'path': str(root.joinpath(*parts)), 'sha256': digest(data),
                  'bytes': len(data), 'total_chars': len(text), 'offset': offset,
                  'content': text[offset:offset+max_chars],
                  'next_offset': offset+max_chars if offset+max_chars < len(text) else None}
        result['receipt_id'] = self.storage.event('read_file', {key: result[key] for key in ('workspace', 'path', 'sha256')}) if record else None
        return result

    def list(self, workspace, path='.', limit=200):
        if not 1 <= limit <= 500:
            raise ToolError('limit 必须在 1–500 之间。')
        root, parts = self.path(workspace, path)
        with self.directory(root, parts) as fd:
            names = sorted(name for name in os.listdir(fd) if self.full_machine or not blocked(name))
            entries = []
            for name in names[:limit]:
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                kind = 'symlink' if stat.S_ISLNK(info.st_mode) else 'directory' if stat.S_ISDIR(info.st_mode) else 'file'
                entries.append({'name': name, 'type': kind, 'bytes': info.st_size})
        return {'workspace': workspace, 'path': str(root.joinpath(*parts)), 'entries': entries, 'truncated': len(names)>limit}

    def write(self, workspace, path, content, expected_sha256=None, create_parents=False):
        data = content.encode('utf-8')
        return self.write_bytes(workspace, path, data, expected_sha256, create_parents, tool='write_file')

    def write_bytes(self, workspace, path, data, expected_sha256=None, create_parents=False, *, tool='write_image'):
        """Atomic binary write under the same path, version and no-follow rules as text writes."""
        if len(data) > self.maximum:
            raise ToolError('写入内容超过文件大小上限。')
        root, parts = self.path(workspace, path)
        if not parts:
            raise ToolError('请指定文件路径。')
        with self.lock:
            try:
                with self.directory(root, parts[:-1], create_parents) as fd:
                    try:
                        old, mode = self.read_bytes(fd, parts[-1])
                        exists = True
                    except FileNotFoundError:
                        old, mode, exists = b'', 0o600, False
                    previous = digest(old) if exists else None
                    if exists and expected_sha256 != previous:
                        raise ToolError('文件已存在或已变化；先 read_file/read_image，并使用其 sha256 作为 expected_sha256。')
                    if not exists and expected_sha256 is not None:
                        raise ToolError('原文件已不存在，拒绝按旧版本覆盖。')
                    temporary = '.localpilot-write-' + uuid.uuid4().hex
                    descriptor = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, mode, dir_fd=fd)
                    try:
                        with os.fdopen(descriptor, 'wb') as output:
                            output.write(data)
                            output.flush()
                            os.fsync(output.fileno())
                        if exists:
                            current, _ = self.read_bytes(fd, parts[-1])
                            if digest(current) != expected_sha256:
                                raise ToolError('写入前文件发生变化；请重新读取。')
                            os.replace(temporary, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
                        else:
                            # Atomic create, refusing a file created by another actor meanwhile.
                            os.link(temporary, parts[-1], src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                        os.fsync(fd)
                    finally:
                        try:
                            os.unlink(temporary, dir_fd=fd)
                        except FileNotFoundError:
                            pass
            except OSError as exc:
                raise ToolError('写入失败；检查路径、权限和并发修改。不会跟随符号链接。') from exc
        result = {'workspace': workspace, 'path': str(root.joinpath(*parts)), 'status': 'updated' if exists else 'created',
                  'previous_sha256': previous, 'sha256': digest(data), 'bytes': len(data)}
        result['receipt_id'] = self.storage.event(tool, result)
        return result

    def _stat(self, workspace, path):
        """Existence check under the same path rules; None when the entry or one of its parents is missing."""
        root, parts = self.path(workspace, path)
        if not parts:
            raise ToolError('请指定文件路径。')
        try:
            return os.lstat(root.joinpath(*parts))
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as exc:
            raise self.io_error(exc) from exc

    def _read_text(self, workspace, path):
        """Whole UTF-8 file (bounded by max_file_bytes) with its hash, for patching."""
        root, parts = self.path(workspace, path)
        if not parts:
            raise ToolError('请指定文件路径。')
        try:
            with self.directory(root, parts[:-1]) as fd:
                data, _ = self.read_bytes(fd, parts[-1])
        except OSError as exc:
            raise self.io_error(exc) from exc
        try:
            return data.decode('utf-8'), digest(data)
        except UnicodeError as exc:
            raise ToolError(f'{path} 不是有效的 UTF-8 文本，不能应用文本补丁。') from exc

    def _unlink(self, workspace, path, expected_sha256):
        root, parts = self.path(workspace, path)
        try:
            with self.directory(root, parts[:-1]) as fd:
                data, _ = self.read_bytes(fd, parts[-1])
                if digest(data) != expected_sha256:
                    raise ToolError(f'{path} 在删除前发生变化；请重新读取。')
                os.unlink(parts[-1], dir_fd=fd)
        except OSError as exc:
            raise self.io_error(exc) from exc

    def apply_patch(self, workspace, patch, expected_sha256=None, create_parents=True):
        """Apply a multi-file, multi-hunk patch atomically per file; all hunks are matched before any write."""
        from patching import parse_patch, apply_hunks
        ops = parse_patch(patch)
        targets = [op['path'] for op in ops if op['kind'] != 'add']
        if isinstance(expected_sha256, str):
            if len(targets) != 1:
                raise ToolError('多文件补丁的 expected_sha256 请使用 {路径: sha256} 字典。')
            expected = {targets[0]: expected_sha256}
        elif isinstance(expected_sha256, dict):
            expected = {str(k): str(v) for k, v in expected_sha256.items()}
        elif expected_sha256 is None:
            expected = {}
        else:
            raise ToolError('expected_sha256 必须是字符串、{路径: sha256} 字典或省略。')
        with self.lock:
            planned, seen = [], set()
            for op in ops:
                if op['path'] in seen:
                    raise ToolError(f'补丁对 {op["path"]} 有重复操作。')
                seen.add(op['path'])
                if op['kind'] == 'add':
                    if self._stat(workspace, op['path']) is not None:
                        raise ToolError(f'{op["path"]} 已存在；修改现有文件请使用 Update File。')
                    if op.get('hunks'):
                        content, stats = apply_hunks('', op['hunks'], op['path'])
                    else:
                        content = '\n'.join(op['lines']) + ('\n' if op['lines'] else '')
                        stats = {'lines_added': len(op['lines']), 'lines_removed': 0, 'hunks_applied': 0}
                    planned.append({'action': 'created', 'path': op['path'], 'content': content, 'previous': None, 'stats': stats})
                    continue
                if op['kind'] == 'delete':
                    current = self.fingerprint(workspace, op['path'], record=False)
                    if op['path'] in expected and expected[op['path']] != current['sha256']:
                        raise ToolError(f'{op["path"]} 的版本与 expected_sha256 不符；请重新读取。')
                    stats = {'lines_added': 0, 'lines_removed': None, 'hunks_applied': 0}
                    if op.get('hunks'):
                        text, sha = self._read_text(workspace, op['path'])
                        remaining, stats = apply_hunks(text, op['hunks'], op['path'])
                        if remaining or stats['lines_added'] or sha != current['sha256']:
                            raise ToolError(f'{op["path"]} 的删除补丁必须与当前文件全部内容一致。')
                    planned.append({'action': 'deleted', 'path': op['path'], 'content': None, 'previous': current['sha256'], 'stats': stats})
                    continue
                text, sha = self._read_text(workspace, op['path'])
                if op['path'] in expected and expected[op['path']] != sha:
                    raise ToolError(f'{op["path"]} 的版本与 expected_sha256 不符；请重新读取后再打补丁。')
                new_text, stats = apply_hunks(text, op['hunks'], op['path']) if op['hunks'] else (text, {'hunks_applied': 0, 'lines_added': 0, 'lines_removed': 0})
                if op.get('move_to'):
                    if op['move_to'] in seen or self._stat(workspace, op['move_to']) is not None:
                        raise ToolError(f'移动目标 {op["move_to"]} 已存在。')
                    seen.add(op['move_to'])
                    planned.append({'action': 'moved', 'path': op['path'], 'move_to': op['move_to'], 'content': new_text, 'previous': sha, 'stats': stats})
                else:
                    planned.append({'action': 'updated', 'path': op['path'], 'content': new_text, 'previous': sha, 'stats': stats})
            # Validate predictable failures before committing any part of a batch.
            canonical = set()
            for item in planned:
                for path in [item['path']] + ([item['move_to']] if item.get('move_to') else []):
                    root, parts = self.path(workspace, path)
                    key = str(root.joinpath(*parts))
                    if key in canonical:
                        raise ToolError(f'补丁中的路径指向同一个文件：{path}')
                    canonical.add(key)
                if item['content'] is not None and len(item['content'].encode('utf-8')) > self.maximum:
                    raise ToolError(f'{item["path"]} 的写入内容超过文件大小上限；尚未写入任何文件。')
            results = []
            for item in planned:
                data = item['content'].encode('utf-8') if item['content'] is not None else None
                if item['action'] == 'created':
                    written = self.write_bytes(workspace, item['path'], data, None, create_parents, tool='apply_patch')
                elif item['action'] == 'updated':
                    written = self.write_bytes(workspace, item['path'], data, item['previous'], False, tool='apply_patch')
                elif item['action'] == 'moved':
                    written = self.write_bytes(workspace, item['move_to'], data, None, create_parents, tool='apply_patch')
                    self._unlink(workspace, item['path'], item['previous'])
                else:
                    self._unlink(workspace, item['path'], item['previous'])
                    root, parts = self.path(workspace, item['path'])
                    written = {'path': str(root.joinpath(*parts)), 'sha256': None, 'bytes': 0}
                results.append({'action': item['action'], 'path': written['path'], 'previous_sha256': item['previous'],
                                'sha256': written.get('sha256'), 'bytes': written.get('bytes', 0), **item['stats'],
                                **({'moved_from': str(self.path(workspace, item['path'])[0].joinpath(*self.path(workspace, item['path'])[1]))} if item['action'] == 'moved' else {})})
        summary = ', '.join(f"{Path(r['path']).name} {r['action']}" for r in results)
        result = {'workspace': workspace, 'files': results, 'summary': summary,
                  'lines_added': sum(r['lines_added'] or 0 for r in results), 'lines_removed': sum(r['lines_removed'] or 0 for r in results)}
        result['receipt_id'] = self.storage.event('apply_patch', {'workspace': workspace, 'files': [{k: r[k] for k in ('action', 'path', 'previous_sha256', 'sha256')} for r in results]})
        return result

    def replace(self, workspace, path, old_text, new_text, expected_sha256):
        if not old_text:
            raise ToolError('old_text 不能为空。')
        with self.lock:
            original = self.read(workspace, path, 0, 64000)
            if original['next_offset'] is not None:
                raise ToolError('文件较长；请使用完整读取后 write_file 更新。')
            if original['sha256'] != expected_sha256:
                raise ToolError('文件版本已变化；请重新 read_file。')
            if original['content'].count(old_text) != 1:
                raise ToolError('old_text 必须在文件中恰好出现一次，请提供更多上下文。')
            return self.write(workspace, path, original['content'].replace(old_text, new_text, 1), expected_sha256)

    def search(self, workspace, pattern='*', query=None, limit=50, path='.'):
        import time
        root, parts = self.path(workspace, path)
        with self.directory(root, parts):
            pass
        root = root.joinpath(*parts)
        deadline, visited = time.monotonic() + 10, 0
        if not 1 <= limit <= 100 or (query is not None and not query):
            raise ToolError('limit 必须在 1–100 之间，query 不能是空字符串。')
        results, scanned, truncated = [], 0, False
        errors, error_count = [], 0
        def skipped(exc, known_path=None):
            nonlocal error_count
            error_count += 1
            if len(errors) < 20:
                errors.append({'path': known_path or exc.filename, 'error': str(self.io_error(exc))})
        # fwalk uses no-follow traversal; read each file relative to its open directory.
        for parent, directories, names, fd in os.fwalk(root, follow_symlinks=False, onerror=skipped):
            visited += 1
            if visited > 10000 or time.monotonic() >= deadline:
                truncated = True
                break
            directories[:] = [name for name in directories if name not in (SKIP_SEARCH - BLOCKED if self.full_machine else SKIP_SEARCH)
                              and not (self.full_machine and any((Path(parent)/name).is_relative_to(p) for p in self.protected))
                              and (self.full_machine or not blocked(name))]
            for name in directories[:]:
                try:
                    if stat.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                        directories.remove(name)
                        continue
                    probe = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(probe)
                except OSError as exc:
                    skipped(exc, str(Path(parent)/name))
                    directories.remove(name)
            for name in sorted(names):
                if time.monotonic() >= deadline:
                    truncated = True
                    break
                relative = str((Path(parent)/name).relative_to(root))
                if (not self.full_machine and blocked(name)) or not fnmatch.fnmatch(relative, pattern):
                    continue
                try:
                    self.allowed(Path(parent)/name)
                except ToolError:
                    continue
                scanned += 1
                if scanned > 2000:
                    truncated = True
                    break
                entry = {'path': relative}
                if query is not None:
                    try:
                        data, _ = self.read_bytes(fd, name)
                        text = data.decode('utf-8')
                    except (OSError, UnicodeError, ToolError):
                        continue
                    matches = [{'line': index, 'text': line[:500]} for index, line in enumerate(text.splitlines(), 1) if query in line][:5]
                    if not matches:
                        continue
                    entry['matches'] = matches
                results.append(entry)
                if len(results) >= limit:
                    truncated = True
                    break
            if truncated:
                break
        return {'workspace': workspace, 'path': str(root), 'results': results, 'scanned': min(scanned, 2000),
                'visited_directories': visited, 'truncated': truncated, 'errors': errors, 'errors_count': error_count}
