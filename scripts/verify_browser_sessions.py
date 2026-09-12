"""Real Chrome login-state persistence across MCP reconnect and browser restart.

Uses only a temporary profile and a localhost fixture. No real credentials.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import http.server
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

from mcp import Client, StdioServerParameters


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        if self.path == '/login':
            self.server.login_requests += 1
            self.send_header('Set-Cookie', 'lp_session=fixture; Path=/; HttpOnly; SameSite=Lax')
            self.send_header('Set-Cookie', 'lp_persistent=fixture; Path=/; HttpOnly; SameSite=Lax; Max-Age=3600')
        cookie = self.headers.get('Cookie', '')
        body = '<title>Login persistence fixture</title><p>' + ' '.join(
            label + ('_PRESENT' if name + '=fixture' in cookie else '_ABSENT')
            for name, label in [('lp_session', 'SESSION'), ('lp_persistent', 'PERSISTENT')]
        ) + '</p>'
        encoded = body.encode()
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


async def verify(output):
    source = Path(os.environ.get('LOCALPILOT_VERIFY_AGENT_ROOT', Path(__file__).resolve().parents[1]))
    checks = []

    async def data(client, tool, **args):
        result = await client.call_tool(tool, args)
        if result.is_error:
            raise RuntimeError((tool, result.content))
        return result.structured_content

    async def read_state(client, url):
        await data(client, 'browser_navigate', url=url)
        return (await data(client, 'browser_snapshot', mode='text'))['text']

    def check(name, passed):
        checks.append({'name': name, 'passed': bool(passed)})

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.login_requests = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    site = f'http://127.0.0.1:{server.server_address[1]}'
    try:
        with tempfile.TemporaryDirectory(prefix='localpilot-session-') as directory:
            base = Path(directory)
            work = base / 'project'
            work.mkdir()
            config = base / 'config.json'
            profile = base / 'profile'
            prefs_path = profile / 'Default/Preferences'
            prefs_path.parent.mkdir(parents=True)
            # Existing Chrome startup preference, deliberately not session restore.
            # Login persistence must work without rewriting this protected setting.
            prefs_path.write_text(json.dumps({'session': {'restore_on_startup': 5}}))
            config.write_text(json.dumps({'workspaces': {'project': str(work)}, 'state_dir': str(base / 'state'),
                'local_skill_roots': [], 'browser': {'enabled': True, 'headless': True,
                    'cdp_port': server.server_address[1], 'profile_dir': str(profile)}}))
            params = StdioServerParameters(command=sys.executable, args=['-I', str(source / 'agent/server.py')],
                                          env={'LOCALPILOT_CONFIG': str(config)})
            async with Client(params) as client:
                await data(client, 'browser_navigate', url=site + '/login')
                initial = await read_state(client, site + '/state')
                check('session_and_persistent_login_set', 'SESSION_PRESENT' in initial and 'PERSISTENT_PRESENT' in initial)
                first_profile = (await data(client, 'browser_tabs', action='status'))['profile_dir']
            async with Client(params) as client:
                try:
                    reconnected = await read_state(client, site + '/state')
                    check('login_survives_mcp_reconnect', 'SESSION_PRESENT' in reconnected and 'PERSISTENT_PRESENT' in reconnected)
                    stopped = await data(client, 'browser_tabs', action='stop')
                    check('managed_chrome_fully_stopped', stopped['stopped_localpilot_chrome'])
                    restarted = await read_state(client, site + '/state')
                    check('session_login_survives_chrome_restart', 'SESSION_PRESENT' in restarted)
                    check('persistent_login_survives_chrome_restart', 'PERSISTENT_PRESENT' in restarted)
                    check('same_profile_reused', (await data(client, 'browser_tabs', action='status'))['profile_dir'] == first_profile)
                    prefs = json.loads((profile / 'Default/Preferences').read_text())
                    check('startup_preferences_preserved', prefs.get('session', {}).get('restore_on_startup') == 5)
                    check('login_not_silently_repeated', server.login_requests == 1)
                finally:
                    await data(client, 'browser_tabs', action='stop')
    finally:
        server.shutdown()
        server.server_close()
    report = {'at': datetime.now(timezone.utc).isoformat(), 'source': str(source),
              'passed': sum(c['passed'] for c in checks), 'total': len(checks), 'checks': checks}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False))
    return all(c['passed'] for c in checks)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output)) else 1)
