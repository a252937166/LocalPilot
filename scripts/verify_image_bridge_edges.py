"""Regression tests for actual image bytes, connection reuse/cancellation and stable DOM refs.

All MCP servers, Chrome profiles and pages are temporary local fixtures.
"""
import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import random
import sys
import tempfile
import time

from PIL import Image

ROOT = Path(os.environ.get('LOCALPILOT_VERIFY_AGENT_ROOT', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / 'agent'))
from browser_control import BrowserControl, _encode_image
from mcp_bridge import Bridge, CallControl, _shrink_image, _content_of
from mcp.types import ImageContent
from storage import Storage

FIXTURE = '''import os,asyncio
from mcp.server import MCPServer
mcp=MCPServer('Temporary bridge fixture')
@mcp.tool()
async def process(wait:float=0)->dict[str,int]:
    await asyncio.sleep(wait)
    return {'pid':os.getpid()}
if __name__=='__main__':mcp.run()
'''


def verify(output):
    checks = []
    def check(name, passed, detail=None):
        checks.append({'name': name, 'passed': bool(passed), **({'detail': detail} if detail is not None else {})})
    def rejects(fn):
        try: fn()
        except Exception: return True
        return False
    noise = Image.frombytes('RGB', (4096, 4096), random.Random(72).randbytes(4096*4096*3))
    encoded = _encode_image(noise)
    check('screenshot_noise_is_bounded', len(encoded[0]) <= 1048576, len(encoded[0]))
    with Image.open(io.BytesIO(encoded[0])) as actual:
        check('encoded_dimensions_are_reported', len(encoded) == 3 and encoded[2] == actual.size)
    bad = base64.b64encode(b'not an image').decode()
    check('invalid_pixels_are_rejected', rejects(lambda: _shrink_image(bad, 'image/png')))
    text, images = _content_of([ImageContent(type='image', data=bad, mime_type='image/png')])
    check('corrupt_remote_image_is_not_forwarded', not images and 'could not be decoded' in text)
    buff = io.BytesIO(); Image.new('RGB', (30, 30), 'red').save(buff, 'PNG')
    corrected = _shrink_image(base64.b64encode(buff.getvalue()).decode(), 'image/jpeg')
    check('remote_mime_matches_actual_pixels', corrected[1] == 'image/png')
    text, images = _content_of([ImageContent(type='image', data=corrected[0], mime_type='image/png')]*8)
    check('image_count_and_descriptions_match_attached_limit', len(images) == 6 and '2 additional images omitted' in text and '[image 7:' not in text)

    with tempfile.TemporaryDirectory(prefix='lp-image-bridge-edges-') as directory:
        root = Path(directory).resolve(); (root/'state').mkdir(); storage = Storage(root/'state')
        fixture = root/'fixture.py'; fixture.write_text(FIXTURE)
        cfg = {'enabled': True, 'config_files': [], 'servers': {
            name: {'command': sys.executable, 'args': [str(fixture)]} for name in ('alpha', 'beta')},
            'allow': ['*'], 'deny': [], 'idle_seconds': 300, 'call_timeout_seconds': 20, 'max_output_chars': 65536}
        bridge = Bridge({'mcp_bridge': cfg}, storage)
        clients = []
        connect = bridge._connect
        async def tracked_connect(*args, **kwargs):
            session = await connect(*args, **kwargs)
            if session['client'] not in clients: clients.append(session['client'])
            return session
        bridge._connect = tracked_connect
        try:
            a = bridge.call('alpha', 'process')['structured_content']['pid']
            again = bridge.call('alpha', 'process')['structured_content']['pid']
            b = bridge.call('beta', 'process')['structured_content']['pid']
            check('stdio_connection_reused', a == again)
            check('servers_have_distinct_cache_keys', set(bridge.sessions) == {'alpha', 'beta'} and a != b, list(bridge.sessions))
            initial = CallControl(scope='task-one')
            pid = bridge.call('alpha', 'process', _control=initial)['structured_content']['pid']
            check('task_session_has_own_key', 'alpha::task-one' in bridge.sessions and pid != a)
            stopped = CallControl(scope='task-one')
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(bridge.call, 'alpha', 'process', {'wait': 15}, _control=stopped)
                until = time.monotonic() + 5
                while stopped.task is None and time.monotonic() < until: time.sleep(.02)
                time.sleep(.15); stopped.cancel()
                check('cancellation_returns_error', rejects(lambda: pending.result(8)))
            check('cancelled_task_session_removed', 'alpha::task-one' not in bridge.sessions)
            def alive(pid):
                try: os.kill(pid, 0); return True
                except ProcessLookupError: return False
            check('cancelled_stdio_process_exited', not alive(pid))
            check('other_session_survives_cancel', bridge.call('alpha', 'process')['structured_content']['pid'] == a)
            before = len(clients)
            control = CallControl(scope='expired')
            check('expired_call_rejected_before_start', rejects(lambda: bridge.call('beta', 'process', _control=control, _deadline=time.time()-1)) and len(clients) == before and control.stop_confirmed)
        finally:
            async def close():
                for client in clients:
                    try: await client.__aexit__(None, None, None)
                    except Exception: pass
            if bridge.loop:
                asyncio.run_coroutine_threadsafe(close(), bridge.loop).result(15)
                bridge.loop.call_soon_threadsafe(bridge.loop.stop)

        chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
        browser_cfg = {'enabled': True, 'cdp_port': 9222, 'cdp_url': None,
            'profile_dir': str(root/'profile'), 'chrome_path': chrome, 'headless': True,
            'launch_if_missing': True, 'navigation_timeout_ms': 5000, 'snapshot_max_chars': 12000,
            'screenshot_max_side': 1600, 'user_chrome_dir': str(root/'fake-user')}
        browser_settings = {'browser': browser_cfg, 'state_dir': str(root/'state'), 'workspaces': {'fixture': str(root)}}
        browser = BrowserControl(browser_settings, storage)
        fresh = external = None
        try:
            fixture = root/'page.html'
            fixture.write_text('<button id="original" onclick="document.title=this.id">Original</button>')
            browser.tabs()
            opened_tab = browser.tabs('open', url=fixture.as_uri())['active']
            browser.tabs('close', tab_id='t1')
            opened = browser.snapshot(tab_id=opened_tab)
            import re
            ref = re.search(r'\[(e\d+)\] button "Original"', opened['snapshot'])[1]
            browser.evaluate('() => {const b=document.querySelector("button");const c=b.cloneNode(true);c.id="clone";b.before(c);}')
            check('ambiguous_cloned_ref_rejected', rejects(lambda: browser.click(ref=ref, snapshot=False)))
            snap = browser.snapshot()
            refs = re.findall(r'\[(e\d+)\]', snap['snapshot'])
            check('cloned_nodes_receive_distinct_refs', len(refs) == len(set(refs)) == 2)
            result = browser.click(ref=ref, snapshot=False)
            check('original_ref_keeps_original_node', result['title'] == 'original')
            browser.shutdown()
            fresh = BrowserControl(browser_settings, storage)
            reconnected = fresh.snapshot()
            check('tab_identity_survives_restart_and_closed_tabs', reconnected['tab_id'] == opened_tab)
            check('closed_tab_id_not_reused_after_restart', rejects(lambda: fresh.snapshot(tab_id='t1')))
            fresh.evaluate('() => {const b=document.createElement("button");b.textContent="New";document.body.append(b);}')
            after = fresh.snapshot()
            refs = re.findall(r'\[(e\d+)\]', after['snapshot'])
            check('refs_unique_after_agent_restart', len(refs) == len(set(refs)) == 3)
            check('zero_image_limits_are_rejected', rejects(lambda: fresh.screenshot(max_side=0)) and rejects(lambda: fresh.screenshot(max_tiles=0)))
            fresh.evaluate('() => {document.body.style.height="100000px";}')
            check('huge_fullpage_rejected_before_capture', rejects(lambda: fresh.screenshot(full_page=True)))
            external = BrowserControl({**browser_settings, 'browser': {**browser_cfg, 'cdp_url': fresh.cdp_url}}, storage)
            external.tabs()
            detached = external.stop()
            check('explicit_external_chrome_is_not_stopped', not detached['stopped_localpilot_chrome'] and fresh._probe() is not None)
            stopped = fresh.stop()
            check('reconnected_agent_can_stop_managed_chrome', stopped['stopped_localpilot_chrome'] and not fresh._probe())
        finally:
            if fresh: fresh.shutdown()
            browser.stop()
            for obj in (browser, fresh, external):
                if obj and obj.loop: obj.loop.call_soon_threadsafe(obj.loop.stop)
    report = {'checks': checks, 'passed': sum(c['passed'] for c in checks), 'total': len(checks), 'agent_root': str(ROOT)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report, ensure_ascii=False))
    return report['passed'] == report['total']


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    sys.exit(0 if verify(parser.parse_args().output) else 1)
