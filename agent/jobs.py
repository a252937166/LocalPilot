"""Supervised shell jobs, with macOS filesystem/network sandboxing."""
from __future__ import annotations
import atexit
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import threading
import time
import uuid
from mcp.server.mcpserver.exceptions import ToolError

OUTPUT_LIMIT = 65536


def sandbox_profile(root: Path, scratch: Path, extra_read: list[str], network: bool, full_machine=False, protected=()) -> str:
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    reads = ['/System', '/Library/Apple', '/Library/Frameworks', '/usr/bin', '/bin', '/sbin',
             '/usr/sbin', '/usr/lib', '/usr/share', '/usr/local/bin', '/usr/local/lib',
             '/usr/local/Cellar', '/usr/local/opt', '/opt/homebrew/bin', '/opt/homebrew/lib',
             '/opt/homebrew/Cellar', '/opt/homebrew/opt', '/private/etc', '/dev',
             str(Path(sys.base_prefix).resolve()), *extra_read]
    lines = ['(version 1)', '(deny default)', '(allow process-exec process-fork sysctl-read)',
             '(allow signal (target same-sandbox))', '(allow file-read-metadata)',
             '(allow file-read* (literal "/"))',
             '(allow mach-lookup (global-name "com.apple.system.logger") (global-name "com.apple.cfprefsd.daemon"))']
    lines.extend(f'(allow file-read* (subpath {quote(path)}))' for path in reads)
    for path in (root, scratch):
        lines.append(f'(allow file-read* file-write* (subpath {quote(str(path))}))')
    lines.extend(['(allow file-write* (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr") (subpath "/dev/fd"))',
                  '(deny file-read* file-write* (regex #"/(\\.env($|[./])|\\.ssh($|/)|\\.aws($|/)|\\.gnupg($|/)|\\.config($|/)|\\.codex($|/)|\\.claude($|/))"))'])
    if network:
        lines.append('(allow network-outbound)')
    if full_machine:
        lines = [line for line in lines if '(regex #' not in line]
        lines.append('(allow file-read* file-write* (subpath "/"))')
        for path in protected:
            lines.append(f'(deny file-read* file-write* (require-all (subpath {quote(str(path))}) (require-not (subpath {quote(str(scratch))}))))')
    return '\n'.join(lines)


def shell_environment(settings, scratch):
    entries = [*settings.get('shell_path_entries', []), '/usr/local/bin', '/opt/homebrew/bin',
               '/usr/bin', '/bin', '/usr/sbin', '/sbin']
    return {**settings.get('shell_env', {}), 'PATH': ':'.join(dict.fromkeys(entries)),
            'HOME': str(Path.home()), 'TMPDIR': str(scratch), 'LANG': 'en_US.UTF-8', 'TERM': 'dumb',
            'PYTHONNOUSERSITE': '1'}


def execution_profile(settings, files, root, scratch):
    if settings.get('shell_permission_mode') == 'full_account':
        if not files.full_machine or settings['shell_network'] is not True:
            raise ToolError('完整账号执行权限必须同时启用 full_machine 和联网。')
        # Even an allow-default Seatbelt profile rejects set-id system tools
        # such as /bin/ps. The explicitly selected account mode adds no profile.
        return None
    return sandbox_profile(root, scratch, settings.get('shell_read_paths', []), bool(settings['shell_network']),
                           files.full_machine, files.protected)


def shell_argv(command, profile):
    argv = ['/bin/zsh', '-f', '-c', command]
    return ['/usr/bin/sandbox-exec', '-p', profile, *argv] if profile is not None else argv


class Jobs:
    def __init__(self, settings, files, storage):
        self.settings, self.files, self.storage = settings, files, storage
        self.lock = threading.RLock()
        self.live = {}
        self.scratch = Path(settings['state_dir']) / 'scratch'
        self.scratch.mkdir(exist_ok=True, mode=0o700)
        atexit.register(self.shutdown)

    def run(self, workspace, command, cwd='.', timeout_seconds=60, request_id=None):
        if not self.settings['shell_enabled']:
            raise ToolError('本机配置没有启用 shell。')
        if platform.system() != 'Darwin' or (self.settings.get('shell_permission_mode') != 'full_account' and not Path('/usr/bin/sandbox-exec').exists()):
            raise ToolError('当前 shell 实现需要 macOS sandbox-exec；不会退回无隔离执行。')
        if not command.strip() or len(command) > 16000 or not 1 <= timeout_seconds <= 300:
            raise ToolError('命令不能为空且最多 16000 字符，超时必须在 1–300 秒之间。')
        if request_id is not None and (not request_id or len(request_id) > 128):
            raise ToolError('request_id 必须为 1–128 个字符。')
        root, parts = self.files.path(workspace, cwd)
        with self.files.directory(root, parts):
            pass
        directory = root.joinpath(*parts)
        fingerprint = hashlib.sha256(json.dumps([workspace, command, str(directory), timeout_seconds]).encode()).hexdigest()
        with self.lock:
            if request_id:
                existing = self.storage.find_request(request_id)
                if existing:
                    if existing[0] != fingerprint:
                        raise ToolError('request_id 已用于不同命令。')
                    return existing[1]
            if len(self.live) >= 4:
                raise ToolError('已有 4 个运行任务；请等待完成或停止已有任务。')
            job_id = uuid.uuid4().hex
            scratch = self.scratch / job_id
            scratch.mkdir(mode=0o700)
            profile = execution_profile(self.settings, self.files, root, scratch)
            environment = shell_environment(self.settings, scratch)
            snapshot = {'job_id': job_id, 'workspace': workspace, 'cwd': str(directory), 'status': 'starting',
                        'started_at': time.time(), 'finished_at': None, 'exit_code': None,
                        'stdout': '', 'stderr': '', 'output_truncated': False, 'timeout_seconds': timeout_seconds,
                        'sandbox': 'macOS Seatbelt' if profile is not None else 'none (macOS account)', 'network_enabled': bool(self.settings['shell_network']),
                        'permission_mode': self.settings.get('shell_permission_mode', 'restricted')}
            self.storage.job(snapshot, request_id, fingerprint)
            try:
                process = subprocess.Popen(shell_argv(command, profile),
                                           cwd=directory, env=environment, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            except OSError as exc:
                snapshot.update(status='failed', stderr='无法启动隔离 shell。', finished_at=time.time())
                self.storage.job(snapshot)
                raise ToolError('无法启动隔离 shell。') from exc
            snapshot['status'] = 'running'
            entry = {'process': process, 'snapshot': snapshot, 'cancel': threading.Event(), 'done': threading.Event()}
            self.live[job_id] = entry
            self.storage.job(snapshot)
            threading.Thread(target=self._watch, args=(job_id, entry), daemon=True).start()
        # Return quickly to ChatGPT; long jobs are queried with job_status.
        entry['done'].wait(0.5)
        return self.status(job_id)

    def _watch(self, job_id, entry):
        process, snapshot = entry['process'], entry['snapshot']
        buffers = {'stdout': bytearray(), 'stderr': bytearray()}
        truncated = threading.Event()

        def drain(name, stream):
            try:
                while True:
                    chunk = os.read(stream.fileno(), 4096)
                    if not chunk:
                        break
                    with self.lock:
                        available = OUTPUT_LIMIT - len(buffers[name])
                        buffers[name].extend(chunk[:available])
                        if len(chunk) > available:
                            truncated.set()
                        snapshot[name] = bytes(buffers[name]).decode('utf-8', errors='replace')
                        snapshot['output_truncated'] = truncated.is_set()
            except (OSError, ValueError):
                pass

        readers = [threading.Thread(target=drain, args=(name, stream), daemon=True)
                   for name, stream in [('stdout', process.stdout), ('stderr', process.stderr)]]
        for reader in readers:
            reader.start()
        outcome = 'completed'
        try:
            process.wait(timeout=snapshot['timeout_seconds'])
        except subprocess.TimeoutExpired:
            outcome = 'timed_out'
            self._kill(process)
            process.wait()
        finally:
            # Do not leave descendants holding pipes or running after their job exits.
            self._kill(process)
            for reader in readers:
                reader.join(timeout=3)
            for stream in (process.stdout, process.stderr):
                stream.close()
            with self.lock:
                if entry['cancel'].is_set():
                    outcome = 'cancelled'
                elif outcome == 'completed' and process.returncode != 0:
                    outcome = 'failed'
                snapshot.update(status=outcome, exit_code=process.returncode, finished_at=time.time())
                snapshot['receipt_id'] = self.storage.event('run_shell', {
                    key: snapshot[key] for key in ('job_id', 'workspace', 'cwd', 'status', 'exit_code')
                })
                self.storage.job(snapshot)
                self.live.pop(job_id, None)
                entry['done'].set()

    @staticmethod
    def _kill(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def status(self, job_id, wait_seconds=0):
        if not isinstance(wait_seconds, (int, float)) or isinstance(wait_seconds, bool) or not 0 <= wait_seconds <= 20:
            raise ToolError('wait_seconds 必须在 0–20 秒之间。')
        with self.lock:
            entry = self.live.get(job_id)
        if entry and wait_seconds:
            # Block briefly for a terminal state; the job keeps running if the wait expires.
            entry['done'].wait(wait_seconds)
        with self.lock:
            if job_id in self.live:
                return dict(self.live[job_id]['snapshot'])
            snapshot = self.storage.get_job(job_id)
        if snapshot is None:
            raise ToolError('找不到 job_id。')
        return snapshot

    def cancel(self, job_id):
        with self.lock:
            entry = self.live.get(job_id)
            if entry:
                entry['cancel'].set()
                self._kill(entry['process'])
        if entry:
            entry['done'].wait(5)
        return self.status(job_id)

    def shutdown(self):
        with self.lock:
            entries = list(self.live.values())
            for entry in entries:
                entry['cancel'].set()
                self._kill(entry['process'])
        for entry in entries:
            entry['done'].wait(5)
