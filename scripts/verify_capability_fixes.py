"""0.7.2 regressions: isolated browser fixtures and real MCP image transport.

No user browser, login stores or model APIs are used.
"""
import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from unittest.mock import patch

from PIL import Image
from mcp import Client, StdioServerParameters

SOURCE = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get('LOCALPILOT_VERIFY_AGENT_ROOT', SOURCE))
sys.path.insert(0, str(ROOT / 'agent'))
from browser_control import BrowserControl
from filesystem import Files
from harness import Harness
from mcp_bridge import CallControl
from storage import Storage


class NoJobs:
    settings = {}
    def run(self, **kwargs): raise AssertionError('No shell in these probes')
    def status(self, job_id): raise AssertionError('No shell in these probes')
    def cancel(self, job_id): raise AssertionError('No shell in these probes')


def browser_settings(base, enabled=True):
    return {'state_dir': str(base / 'state'), 'browser': {
        'enabled': enabled, 'cdp_port': 19222, 'cdp_url': None,
        'profile_dir': str(base / 'browser-profile'), 'chrome_path': '/not-launched',
        'headless': True, 'launch_if_missing': False, 'navigation_timeout_ms': 1000,
        'snapshot_max_chars': 12000, 'screenshot_max_side': 1600,
        'user_chrome_dir': str(base / 'fake-user-chrome')}}


def fake_cookies(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS cookies (host_key TEXT)')
        db.execute('DELETE FROM cookies')
        db.execute('INSERT INTO cookies VALUES (?)', (value,))


FIXTURE = '''
import base64,io
from PIL import Image
from mcp.server import MCPServer
from mcp.types import CallToolResult,ImageContent,TextContent
mcp=MCPServer('Capability image fixture')
@mcp.tool()
def picture()->CallToolResult:
    b=io.BytesIO();Image.new('RGB',(24,24),'green').save(b,format='PNG')
    return CallToolResult(content=[TextContent(type='text',text='Fixture image'),ImageContent(type='image',data=base64.b64encode(b.getvalue()).decode(),mime_type='image/png')])
if __name__=='__main__':mcp.run()
'''


def verify(output):
    checks = []
    def check(name, value):
        checks.append({'name': name, 'passed': bool(value)})
        assert value, name
    def rejects(fn, needle=None):
        try: fn()
        except Exception as exc: return needle is None or needle in str(exc)
        return False
    with tempfile.TemporaryDirectory(prefix='localpilot-capability-fixes-') as temp:
        base = Path(temp); work = base / 'work'; work.mkdir()
        (work / 'done.txt').write_text('done')
        state = base / 'state'; state.mkdir()
        storage = Storage(state)
        files = Files({'workspaces': {'project': str(work)}, 'max_file_bytes': 4096}, storage)
        harness = Harness(files, NoJobs(), storage)
        def task(label):
            return harness.create('project', label,
                [{'id': 'one', 'step': 'Fixture', 'status': 'in_progress'}],
                [{'kind': 'file_equals', 'path': 'done.txt', 'value': 'done'}])

        for stop_state in ('paused', 'cancelled', 'deadline'):
            browser = BrowserControl(browser_settings(base), storage)
            started = threading.Event(); effects = []
            class Page:
                url = 'https://fixture.invalid/'
                async def title(self): return 'Fixture'
            page = Page(); browser.pages = {'t1': page}; browser.active = 't1'
            class Locator:
                async def scroll_into_view_if_needed(self, **kwargs): pass
                async def click(self, **kwargs):
                    started.set()
                    await asyncio.sleep(0.25)
                    effects.append(time.time())
            async def get_page(tab_id=None): return page
            async def resolve(page, ref, selector, text): return Locator(), 'fixture button'
            async def settle(*args, **kwargs): pass
            browser._page = get_page; browser._resolve = resolve; browser._settle = settle
            harness.operations['browser_click'] = browser.click
            t = task('Browser stop probe: ' + stop_state)
            if stop_state == 'deadline':
                with harness.lock:
                    raw = harness._load(t['task_id']); raw['deadline'] = time.time() + 0.08; harness._save(raw)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(harness.execute, t['task_id'], 'one', 'browser_click',
                                      {'selector': '#fixture', 'snapshot': False}, 'click')
                assert started.wait(2), 'Fixture did not start'
                if stop_state != 'deadline': harness.set_state(t['task_id'], stop_state, 'Test stop')
                reply = pending.result(3)
            time.sleep(0.3)
            check('F1_no_delayed_fixture_effect_after_' + stop_state, not effects)
            check('F1_stopped_action_not_success_' + stop_state, reply['action']['status'] == 'interrupted')
            result = reply['action']['result']
            check('F1_truthful_remote_stop_' + stop_state,
                  result['cancel_requested'] and result['call_settled'] and result['cancellation_confirmed'] is False)
            check('F1_control_released_' + stop_state, not harness.bridge_controls)
            browser.loop.call_soon_threadsafe(browser.loop.stop)

        browser = BrowserControl(browser_settings(base), storage)
        started = []
        async def once(): started.append(True)
        control = CallControl(); control.cancel()
        check('F1_cancel_before_dispatch', rejects(lambda: browser._run(once(), 1, control)) and not started and control.stop_confirmed)
        control = CallControl()
        check('F1_expired_before_dispatch', rejects(lambda: browser._run(once(), 1, control, time.time() - 1)) and not started and control.stop_confirmed)
        browser.loop.call_soon_threadsafe(browser.loop.stop)

        disabled = BrowserControl(browser_settings(base, enabled=False), storage)
        fake_cookies(disabled.user_chrome / 'Default/Cookies', 'fake-login.invalid')
        target = disabled.profile_dir / 'Default/Cookies'
        for force in (False, True):
            check('F2_disabled_clone_rejected_' + str(force), rejects(lambda: disabled.tabs(action='clone_logins', force=force), '未启用') and not target.exists())
        check('F2_direct_disabled_clone_rejected', rejects(disabled.clone_logins, '未启用') and not target.exists())

        browser = BrowserControl(browser_settings(base), storage)
        browser._user_chrome_running = lambda: False
        browser._profile_running = lambda path: False
        browser._probe = lambda: {'Browser': 'Fixture running Chrome'}
        fake_cookies(target, 'target-original.invalid')
        def unchanged():
            with sqlite3.connect(target) as db:
                return db.execute('SELECT host_key FROM cookies').fetchone()[0] == 'target-original.invalid'
        check('F3_no_handle_but_CDP_live_rejected', rejects(lambda: browser.clone_logins(force=True), '目标') and unchanged())
        browser._probe = lambda: None
        browser._profile_running = lambda path: True
        check('F3_live_target_process_rejected', rejects(browser.clone_logins, '目标') and unchanged())
        browser._profile_running = lambda path: None
        check('F3_unknown_target_process_rejected', rejects(browser.clone_logins, '目标') and unchanged())
        browser._profile_running = lambda path: False
        lock = browser.profile_dir / 'SingletonLock'; lock.symlink_to('fixture-' + str(os.getpid()))
        check('F3_live_target_lock_rejected', rejects(browser.clone_logins, '目标') and unchanged())
        lock.unlink()
        reply = browser.clone_logins()
        check('F3_stopped_target_clone_works', 'Cookies' in reply['copied'] and not unchanged())

        # Initialization and copying share the same lifecycle lock. An operation
        # must not enter while another one owns the target profile.
        started = threading.Event(); release = threading.Event(); ordering = []
        def slow_clone(*args):
            ordering.append('copy_start'); started.set(); release.wait(2); ordering.append('copy_end'); return {}
        async def connect(): ordering.append('connect')
        with patch.object(browser, '_clone_logins', slow_clone), patch.object(browser, '_connect', connect):
            with ThreadPoolExecutor(max_workers=2) as pool:
                clone = pool.submit(browser.clone_logins)
                assert started.wait(2)
                connection = pool.submit(browser._run, browser._ensure(), 2)
                time.sleep(0.05)
                check('F3_launch_waits_for_clone', ordering == ['copy_start'])
                release.set(); clone.result(3); connection.result(3)
                check('F3_clone_finishes_before_launch', ordering == ['copy_start', 'copy_end', 'connect'])
        browser.loop.call_soon_threadsafe(browser.loop.stop)

        buffer = io.BytesIO(); Image.new('RGB', (16, 16), 'green').save(buffer, format='PNG')
        original = base64.b64encode(buffer.getvalue()).decode()
        calls = []
        def screenshot(*, _control=None, _deadline=None):
            calls.append(True)
            return {'tab_id': 't1', 'images': [{'data': original, 'mime_type': 'image/png'}]}
        harness.operations['browser_screenshot'] = screenshot
        t = task('Screenshot replay'); tid = t['task_id']
        first = harness.execute(tid, 'one', 'browser_screenshot', {}, 'shot', observe=True)
        replay = harness.execute(tid, 'one', 'browser_screenshot', {}, 'shot', observe=True)
        check('F4_screenshot_replay_same_pixels', first['images'] == replay['images'] and replay['replayed'] and len(calls) == 1)
        with storage.lock:
            saved = storage.db.execute('SELECT snapshot FROM task_actions WHERE task_id=?', (tid,)).fetchone()[0]
        check('F4_no_base64_in_SQLite', original not in saved and 'image_attachments' in saved)
        storage.db.close(); storage = Storage(state)
        files = Files({'workspaces': {'project': str(work)}, 'max_file_bytes': 4096}, storage)
        harness = Harness(files, NoJobs(), storage); harness.operations['browser_screenshot'] = screenshot
        replay = harness.execute(tid, 'one', 'browser_screenshot', {}, 'shot', observe=True)
        check('F4_restart_replay_same_pixels', first['images'] == replay['images'] and len(calls) == 1)
        ref = first['action']['result']['image_attachments'][0]
        artifact = state / 'task-images' / ref['sha256']
        check('F4_private_attachment_permissions', artifact.stat().st_mode & 0o777 == 0o600)
        artifact.write_bytes(b'changed')
        check('F4_corrupt_attachment_explicit_error', rejects(lambda: harness.execute(tid, 'one', 'browser_screenshot', {}, 'shot', observe=True), '校验失败') and len(calls) == 1)
        artifact.unlink()
        check('F4_missing_attachment_explicit_error', rejects(lambda: harness.execute(tid, 'one', 'browser_screenshot', {}, 'shot', observe=True), '缺失') and len(calls) == 1)

        browser = BrowserControl(browser_settings(base), storage)
        connects = []
        class ConnectedPage:
            def once(self, event, callback): pass
            def on(self, event, callback): pass
            def is_closed(self): return False
        class Context:
            def __init__(self): self.pages = [ConnectedPage()]
            def set_default_timeout(self, value): pass
            def on(self, event, callback): pass
            async def new_cdp_session(self, page):
                class Session:
                    async def send(self, method): return {'targetInfo': {'targetId': 'fixture-target'}}
                    async def detach(self): pass
                return Session()
        class Connected:
            def __init__(self): self.contexts = [Context()]
            def is_connected(self): return True
        class Chromium:
            async def connect_over_cdp(self, *args, **kwargs):
                connects.append(True); await asyncio.sleep(0.05); return Connected()
        class Playwright: chromium = Chromium()
        browser.playwright = Playwright(); browser._probe = lambda: {'Browser': 'Fixture'}
        async def request_page():
            page = await browser._page(); await asyncio.sleep(0.08); return browser._tab_id(page)
        async def concurrent(): return await asyncio.gather(request_page(), request_page())
        returned_ids = asyncio.run(concurrent())
        check('F5_concurrent_first_use_one_connection', len(connects) == 1)
        check('F5_inflight_page_keeps_tab_id', returned_ids[0] == returned_ids[1] and returned_ids[0] is not None)
        storage.db.close()

        async def protocol():
            fixture = base / 'fixture.py'; fixture.write_text(FIXTURE)
            config = base / 'config.json'; config.write_text(json.dumps({'workspaces': {'project': str(work)}, 'state_dir': str(base / 'protocol-state'),
                'local_skill_roots': [], 'global_instruction_files': [], 'browser': {'enabled': False},
                'mcp_bridge': {'enabled': True, 'config_files': [], 'servers': {'images': {'command': sys.executable, 'args': [str(fixture)]}}}}))
            params = StdioServerParameters(command=sys.executable, args=['-I', str(ROOT / 'agent/server.py')], env={'LOCALPILOT_CONFIG': str(config)})
            async def call(client, name, **args):
                result = await client.call_tool(name, args)
                assert not result.is_error, (name, result.content)
                return result
            async with Client(params) as client:
                status = (await call(client, 'device_status')).structured_content
                check('Chat_screenshot_fallback_is_discoverable', 'inspect_task_step' in status['tool_fallbacks']['browser_screenshot'])
                t = (await call(client, 'create_task', workspace='project', objective='Check remote image transport',
                    plan=[{'id': 'one', 'step': 'Read fixture image', 'status': 'in_progress'}],
                    checks=[{'kind': 'file_equals', 'path': 'done.txt', 'value': 'done'}])).structured_content
                args = {'task_id': t['task_id'], 'step_id': 'one', 'operation': 'call_mcp_tool',
                        'arguments': {'server': 'images', 'tool': 'picture', 'arguments': {}}, 'action_id': 'picture'}
                first = await call(client, 'run_task_step', **args)
                replay = await call(client, 'run_task_step', **args)
                pixels = lambda r: [c.data for c in r.content if c.type == 'image']
                expected = pixels(first)
                check('F4_real_MCP_replay_returns_original_image', len(expected) == 1 and expected == pixels(replay) and replay.structured_content['replayed'])
                result = await client.call_tool('browser_tabs', {'action': 'clone_logins', 'force': True})
                check('F2_real_MCP_disabled_clone_rejected', result.is_error)
            async with Client(params) as client:
                replay = await call(client, 'run_task_step', **args)
                check('F4_real_MCP_restart_replay', expected == pixels(replay) and replay.structured_content['replayed'])
        asyncio.run(protocol())

    from config import VERSION
    manifest = json.loads((SOURCE / 'plugins/localpilot/.codex-plugin/plugin.json').read_text())
    check('F6_manifest_version_matches_agent', manifest['version'] == VERSION)
    for rel in ('plugins/localpilot/skills/local-control/SKILL.md', 'README.zh-CN.md'):
        body = (SOURCE / rel).read_text()
        check('F7_no_stale_browser_capability_denial_' + Path(rel).name,
              '没有截屏、浏览器' not in body and '不提供截屏、浏览器控制' not in body and 'Chrome 页面快照' in body)
    report = {'scope': __doc__, 'passed': sum(c['passed'] for c in checks), 'total': len(checks), 'checks': checks}
    Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'total': report['total'], 'output': str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    verify(parser.parse_args().output)
