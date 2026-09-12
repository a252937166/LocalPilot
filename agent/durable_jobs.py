"""Detached local workers: an MCP connection owns requests, not process lifetimes."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time
import uuid
from mcp.server.mcpserver.exceptions import ToolError
from jobs import execution_profile, shell_environment

ACTIVE = ('starting', 'running')


class DurableJobs:
    def __init__(self, settings, files, storage):
        self.settings, self.files, self.storage = settings, files, storage
        self.lock = threading.RLock()
        self.runners = Path(settings['state_dir']) / 'runners'
        self.scratch = Path(settings['state_dir']) / 'scratch'
        for p in (self.runners, self.scratch):
            p.mkdir(exist_ok=True, mode=0o700)

    def run(self, workspace, command, cwd='.', timeout_seconds=None, request_id=None):
        timeout_seconds = self.settings['default_shell_timeout_seconds'] if timeout_seconds is None else timeout_seconds
        maximum = self.settings['max_shell_timeout_seconds']
        if not self.settings['shell_enabled']:
            raise ToolError('本机配置没有启用 shell。')
        if platform.system() != 'Darwin' or (self.settings.get('shell_permission_mode') != 'full_account' and not Path('/usr/bin/sandbox-exec').exists()):
            raise ToolError('长任务需要 macOS sandbox-exec。')
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= maximum or not command.strip() or len(command) > 16000:
            raise ToolError(f'命令不能为空且最多 16000 字符，超时必须在 1–{maximum} 秒之间。')
        if request_id is not None and (not request_id or len(request_id) > 128):
            raise ToolError('request_id 必须为 1–128 个字符。')
        root, parts = self.files.path(workspace, cwd)
        with self.files.directory(root, parts):
            pass
        directory = root.joinpath(*parts)
        fingerprint = hashlib.sha256(json.dumps([workspace, command, str(directory), timeout_seconds]).encode()).hexdigest()
        with self.lock:
            if request_id:
                previous = self.storage.find_request(request_id)
                if previous:
                    if previous[0] != fingerprint:
                        raise ToolError('request_id 已用于不同命令。')
                    return self.status(previous[1]['job_id'])
            with self.storage.lock:
                ids = [row[0] for row in self.storage.db.execute('SELECT id FROM jobs')]
            if sum(self.status(i)['status'] in ACTIVE for i in ids) >= 4:
                raise ToolError('已有 4 个运行任务；请等待完成或停止已有任务。')
            job_id = uuid.uuid4().hex
            control, scratch = self.runners / job_id, self.scratch / job_id
            control.mkdir(mode=0o700); scratch.mkdir(mode=0o700)
            profile = execution_profile(self.settings, self.files, root, scratch)
            environment = shell_environment(self.settings, scratch)
            snapshot = {'job_id': job_id, 'workspace': workspace, 'cwd': str(directory), 'status': 'starting',
                        'started_at': time.time(), 'finished_at': None, 'exit_code': None, 'stdout': '', 'stderr': '',
                        'output_truncated': False, 'output_strategy': 'tail', 'timeout_seconds': timeout_seconds,
                        'sandbox': 'macOS Seatbelt' if profile is not None else 'none (macOS account)', 'network_enabled': self.settings['shell_network'], 'detached': True,
                        'permission_mode': self.settings.get('shell_permission_mode', 'restricted')}
            self.storage.job(snapshot, request_id, fingerprint)
            launch = control / 'launch.json'
            with launch.open('x') as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump({'state_dir': self.settings['state_dir'], 'snapshot': snapshot, 'profile': profile,
                           'command': command, 'environment': environment,
                           'prevent_idle_sleep': self.settings.get('prevent_idle_sleep', True)}, f)
            try:
                # No pipe or process group connects the worker lifetime to this MCP session.
                prefix = ['/usr/bin/arch', '-' + platform.machine()]
                with (control / 'worker.log').open('ab') as log:
                    process = subprocess.Popen([*prefix, sys.executable, '-I', str(Path(__file__).with_name('job_worker.py')), str(launch)],
                                               stdin=subprocess.DEVNULL, stdout=log, stderr=log, env={'PATH': '/usr/bin:/bin'},
                                               start_new_session=True, close_fds=True)
                threading.Thread(target=process.wait, daemon=True).start()
            except OSError as exc:
                snapshot.update(status='failed', finished_at=time.time(), stderr=str(exc))
                self.storage.job(snapshot)
                raise ToolError('无法启动本机长任务 worker。') from exc
        time.sleep(0.5)
        return self.status(job_id)

    def status(self, job_id, wait_seconds=0):
        if type(wait_seconds) not in (int, float) or not 0 <= wait_seconds <= 20:
            raise ToolError('wait_seconds 必须在 0–20 秒之间。')
        end = time.monotonic() + wait_seconds
        while True:
            snapshot = self.storage.get_job(job_id)
            if snapshot is None:
                raise ToolError('找不到 job_id。')
            stale = time.time() - snapshot.get('heartbeat_at', snapshot['started_at']) > 15
            if snapshot['status'] in ACTIVE and snapshot.get('detached') and stale:
                pid = snapshot.get('runner_pid')
                alive = False
                if pid:
                    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'stat=', '-o', 'command='], capture_output=True, text=True)
                    alive = result.returncode == 0 and not result.stdout.lstrip().startswith('Z') and str(self.runners / job_id / 'launch.json') in result.stdout
                if not alive:
                    with self.lock:
                        current = self.storage.get_job(job_id)
                        if current['status'] in ACTIVE and current.get('heartbeat_at') == snapshot.get('heartbeat_at'):
                            current.update(status='interrupted', finished_at=time.time(), stderr=current.get('stderr', '') + '\nWorker exited unexpectedly; inspect actual files before retrying.')
                            self.storage.job(current)
                        snapshot = current
            if snapshot['status'] not in ACTIVE or time.monotonic() >= end:
                snapshot['elapsed_seconds'] = round((snapshot.get('finished_at') or time.time()) - snapshot['started_at'], 1)
                return snapshot
            time.sleep(min(0.2, max(0, end - time.monotonic())))

    def cancel(self, job_id):
        snapshot = self.status(job_id)
        if snapshot['status'] in ACTIVE:
            if not snapshot.get('detached'):
                raise ToolError('旧会话作业无法通过新 worker 接管，请检查实际进程。')
            (self.runners / job_id / 'cancel').touch(mode=0o600, exist_ok=True)
            return self.status(job_id, wait_seconds=10)
        return snapshot

    def shutdown(self):
        # Disconnecting/restarting MCP must not cancel a user's long-running work.
        pass
