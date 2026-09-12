"""One independently supervised shell job; writes durable heartbeats and bounded output."""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from storage import Storage


def main(launch):
    launch = Path(launch)
    spec = json.loads(launch.read_text())
    storage = Storage(spec['state_dir'], recover=False)
    snapshot = spec['snapshot']
    snapshot.update(runner_pid=os.getpid(), heartbeat_at=time.time())
    cancel = launch.parent / 'cancel'
    lock = threading.RLock()
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    totals = {'stdout': 0, 'stderr': 0}
    limit = 65536
    process = None
    awake = None
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())

    def persist():
        with lock:
            for name in buffers:
                snapshot[name] = bytes(buffers[name]).decode('utf-8', errors='replace')
                snapshot[name + '_bytes'] = totals[name]
            snapshot['output_truncated'] = any(n > limit for n in totals.values())
            snapshot['heartbeat_at'] = time.time()
            storage.job(snapshot)

    def drain(name, stream):
        try:
            while chunk := os.read(stream.fileno(), 8192):
                with lock:
                    totals[name] += len(chunk)
                    buffers[name].extend(chunk)
                    del buffers[name][:-limit]
        except (OSError, ValueError):
            pass

    def kill():
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    outcome, readers = 'failed', []
    try:
        if cancel.exists():
            outcome = 'cancelled'
        else:
            if spec.get('prevent_idle_sleep') and Path('/usr/bin/caffeinate').exists():
                awake = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                snapshot['caffeinate_pid'] = awake.pid
            argv = ['/bin/zsh', '-f', '-c', spec['command']]
            if spec['profile'] is not None:
                argv = ['/usr/bin/sandbox-exec', '-p', spec['profile'], *argv]
            process = subprocess.Popen(argv,
                                       cwd=snapshot['cwd'], env=spec['environment'], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            snapshot.update(status='running', command_pid=process.pid)
            storage.job(snapshot)
            readers = [threading.Thread(target=drain, args=(name, stream), daemon=True) for name, stream in [('stdout', process.stdout), ('stderr', process.stderr)]]
            for reader in readers:
                reader.start()
            deadline = time.monotonic() + max(0, snapshot['timeout_seconds'] - (time.time() - snapshot['started_at']))
            next_save = 0
            while process.poll() is None:
                if cancel.exists() or stopped.is_set():
                    outcome = 'cancelled'; kill(); break
                if time.monotonic() >= deadline:
                    outcome = 'timed_out'; kill(); break
                if time.monotonic() >= next_save:
                    persist(); next_save = time.monotonic() + 2
                time.sleep(0.2)
            else:
                outcome = 'completed' if process.returncode == 0 else 'failed'
            process.wait()
    except Exception as exc:
        with lock:
            buffers['stderr'].extend(str(exc).encode())
    finally:
        kill()
        if process is not None:
            process.wait()
        if awake is not None:
            if awake.poll() is None:
                awake.terminate()
            awake.wait()
        for reader in readers:
            reader.join(timeout=3)
        snapshot.update(status=outcome, exit_code=process.returncode if process else None, finished_at=time.time())
        snapshot['receipt_id'] = storage.event('run_shell', {key: snapshot.get(key) for key in ('job_id', 'workspace', 'cwd', 'status', 'exit_code')})
        persist()
        storage.db.close()


if __name__ == '__main__':
    main(sys.argv[1])
