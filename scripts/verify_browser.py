"""Browser control and MCP image passthrough over real MCP stdio: headless Chrome on a private profile, local test site."""
from __future__ import annotations
import argparse
import asyncio
import base64
from datetime import datetime, timezone
import http.server
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from mcp import Client, StdioServerParameters
from mcp.server.mcpserver.exceptions import ToolError

PAGE = '''<!doctype html><html><head><meta charset="utf-8"><title>LocalPilot Browser Fixture</title></head><body>
<h1>Fixture Home</h1>
<form action="/search" method="get">
  <label for="q">搜索应用</label><input id="q" name="q" placeholder="输入 AppKey">
  <select name="env" aria-label="环境"><option value="prod">生产</option><option value="test">测试</option></select>
  <label><input type="checkbox" name="agree"> 同意</label>
  <button type="submit">搜索</button>
</form>
<a href="/second">第二页</a>
<p id="para">这是一段正文，用于文本模式。</p>
<my-widget></my-widget>
<button id="del" onclick="document.title = confirm('确定删除？') ? 'Deleted' : 'Kept'">删除</button>
<button id="ask" onclick="document.title = 'Name:' + (prompt('名字？', 'x') || '')">询问</button>
<button id="notice" onclick="alert('已保存'); document.title = 'Alerted'">提示</button>
<div id="rows"><button class="row">Row A</button><button class="row">Row B</button></div>
<button id="shift" onclick="[...document.querySelectorAll('.row')].forEach((b, i) => b.textContent = 'Row ' + ['X', 'Y'][i])">换行</button>
<script>
customElements.define('my-widget', class extends HTMLElement { connectedCallback() { const r = this.attachShadow({mode:'open'}); r.innerHTML = '<button id="shadow-btn">影子按钮</button>'; r.querySelector('button').addEventListener('click', () => { document.title = 'Shadow Clicked'; }); } });
document.querySelectorAll('.row').forEach(b => b.addEventListener('click', () => { document.title = 'clicked ' + b.textContent; }));
</script>
</body></html>'''
SECOND = '<!doctype html><html><head><title>Second Page</title></head><body><h2>Second</h2><a href="/">返回首页</a><div style="height:3000px"></div><p id="bottom">底部</p></body></html>'
TALL = '<!doctype html><html><head><title>Tall</title></head><body style="margin:0"><h1>Top</h1><div style="height:5200px;background:linear-gradient(#fff,#999)"></div><p id="end">End</p></body></html>'
SLOW = '<!doctype html><html><head><title>Slow</title></head><body><h1>Slow page</h1><button id="usable">可用按钮</button><img src="/hang" alt=""></body></html>'
FIXTURE_MCP = '''
import base64, io
from mcp.server import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image
mcp = MCPServer('ImageFixture')

@mcp.tool()
def picture(size: int = 64) -> CallToolResult:
    image = Image.new('RGB', (size, size), (200, 30, 30))
    buffer = io.BytesIO(); image.save(buffer, format='PNG')
    return CallToolResult(content=[TextContent(type='text', text='here is the picture'),
                                   ImageContent(type='image', data=base64.b64encode(buffer.getvalue()).decode('ascii'), mime_type='image/png')])

if __name__ == '__main__':
    mcp.run()
'''


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/hang':
            time.sleep(15)
            return
        if self.path.startswith('/search'):
            body = f'<!doctype html><html><head><title>Results</title></head><body><h1>Results for {self.path}</h1><a href="/">Home</a></body></html>'
        elif self.path.startswith('/second'):
            body = SECOND
        elif self.path.startswith('/tall'):
            body = TALL
        elif self.path.startswith('/slow'):
            body = SLOW
        else:
            body = PAGE
        data = body.encode('utf-8')
        self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(data))); self.end_headers()
        self.wfile.write(data)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); return sock.getsockname()[1]


def fake_user_chrome(root):
    """Two Chrome profiles with a Local State that names 'Profile 1' as the one used last."""
    for name, host in (('Default', 'old.example'), ('Profile 1', 'main.example')):
        (root / name / 'Local Storage/leveldb').mkdir(parents=True)
        (root / name / 'Local Storage/leveldb/000001.ldb').write_bytes(b'x')
        with sqlite3.connect(root / name / 'Cookies') as db:
            db.execute('CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)')
            db.execute('INSERT INTO cookies VALUES (?,?,?)', (host, 'sid', 'v'))
    (root / 'Local State').write_text(json.dumps({'profile': {'last_used': 'Profile 1', 'info_cache': {'Default': {'name': '旧号'}, 'Profile 1': {'name': '主号'}}}}))


async def verify(output):
    source = Path(os.environ.get('LOCALPILOT_VERIFY_AGENT_ROOT', Path(__file__).resolve().parents[1]))
    checks = []
    def check(name, value):
        checks.append({'name': name, 'passed': bool(value)})
        assert value, name
    async def call(client, name, **args):
        result = await client.call_tool(name, args)
        assert not result.is_error, (name, result.content)
        return result
    async def data(client, name, **args):
        return (await call(client, name, **args)).structured_content
    async def rejected(client, name, **args):
        return (await client.call_tool(name, args)).is_error
    async def error_text(client, name, **args):
        result = await client.call_tool(name, args)
        return ''.join(getattr(c, 'text', '') for c in result.content) if result.is_error else None
    def ref_of(snapshot, needle):
        for line in snapshot.splitlines():
            if needle in line and line.startswith('['):
                return line[1:line.index(']')]
        raise AssertionError(f'no ref for {needle!r} in:\n{snapshot}')
    def cookie_host(profile_dir):
        with sqlite3.connect(profile_dir / 'Default/Cookies') as db:
            return db.execute('SELECT host_key FROM cookies').fetchone()[0]
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    site = f'http://127.0.0.1:{server.server_address[1]}'
    with tempfile.TemporaryDirectory(prefix='localpilot-browser-') as directory:
        base = Path(directory).resolve(); work = base / 'project'; work.mkdir()
        allowed_file = work / 'allowed-file.txt'; allowed_file.write_text('LOCALPILOT-WORKSPACE-FILE')
        outside_file = base / 'outside-file.txt'; outside_file.write_text('LOCALPILOT-OUTSIDE-FILE')
        linked_file = work / 'linked-file.txt'; linked_file.symlink_to(outside_file)
        hardlinked_file = work / 'hardlinked-file.txt'; os.link(outside_file, hardlinked_file)
        blocked_dir = work / '.ssh'; blocked_dir.mkdir()
        (blocked_dir / 'alias.txt').symlink_to(allowed_file)
        outside_page = base / 'outside.html'
        outside_page.write_text('<title>PRIVATE-TITLE-FIXTURE</title><p>PRIVATE-FRAME-FIXTURE</p>')
        frame_page = work / 'frame.html'
        frame_page.write_text(f'<title>Allowed container</title><iframe src="{outside_page.as_uri()}"></iframe>')
        link_page = work / 'link.html'
        link_page.write_text(f'<title>Local link</title><a href="{outside_page.as_uri()}">Outside fixture</a>')
        fixture = base / 'image_fixture.py'; fixture.write_text(FIXTURE_MCP)
        toml = base / 'codex.toml'; toml.write_text(f'[mcp_servers.pictures]\ncommand = "{sys.executable}"\nargs = ["{fixture}"]\n')
        user_chrome = base / 'userchrome'; fake_user_chrome(user_chrome)
        state, config = base / 'state', base / 'config/config.json'
        config.parent.mkdir()
        cdp_port = server.server_address[1]  # Occupied IPv4 port reproduces the deployed 9222 collision.
        profile_dir = base / 'profile'
        browser_cfg = {'enabled': True, 'headless': True, 'cdp_port': cdp_port, 'profile_dir': str(profile_dir), 'navigation_timeout_ms': 20000, 'user_chrome_dir': str(user_chrome)}
        settings = {'workspaces': {'project': str(work)}, 'state_dir': str(state), 'local_skill_roots': [],
                    'browser': browser_cfg, 'mcp_bridge': {'enabled': True, 'config_files': [str(toml)]}}
        config.write_text(json.dumps(settings))
        params = StdioServerParameters(command=sys.executable, args=['-I', str(source / 'agent/server.py')], env={'LOCALPILOT_CONFIG': str(config)})
        async with Client(params) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            check('browser_tools_registered', all(n in tools for n in ('browser_navigate', 'browser_snapshot', 'browser_click', 'browser_type', 'browser_act', 'browser_screenshot', 'browser_evaluate', 'browser_tabs')) and tools['browser_snapshot'].annotations.read_only_hint and not tools['browser_click'].annotations.read_only_hint)
            check('screenshot_publishes_image_receipt_schema', {'page_url','path','image_metadata','image_sha256','receipt_id'} <= set((tools['browser_screenshot'].output_schema or {}).get('required', [])) and 'url' not in tools['browser_screenshot'].output_schema.get('properties', {}))
            check('value_and_arg_have_typed_schemas', 'anyOf' in tools['browser_act'].input_schema['properties']['value'] and 'anyOf' in tools['browser_evaluate'].input_schema['properties']['arg']
                  and all(any(k in p for k in ('type', 'anyOf', '$ref')) for t in tools.values() for p in t.input_schema.get('properties', {}).values()))
            status = await data(client, 'browser_tabs', action='status')
            check('status_before_connect', status['connected'] is False and status['profile_dir'].endswith('profile'))
            opened = await data(client, 'browser_navigate', url=site + '/')
            check('navigate_launches_chrome_and_snapshots', opened['tab_id'] == 't1' and opened['title'] == 'LocalPilot Browser Fixture' and '[e' in opened['snapshot'] and '搜索应用' in opened['snapshot'] and opened['status'] == 200)
            status = await data(client, 'browser_tabs', action='status')
            check('occupied_port_automatically_uses_free_endpoint', status['chrome_reachable'] and status['cdp_url'] != site and (profile_dir / 'DevToolsActivePort').is_file())
            check('desktop_window_size', opened['viewport']['width'] >= 1200)
            check('device_status_reports_browser', (await data(client, 'device_status'))['browser']['connected'] is True)
            local_page = await data(client, 'browser_navigate', url=allowed_file.as_uri())
            local_text = await data(client, 'browser_snapshot', mode='text')
            check('file_url_inside_workspace_allowed', local_page['url'] == allowed_file.as_uri() and local_text['text'] == 'LOCALPILOT-WORKSPACE-FILE')
            check('file_url_outside_workspace_rejected', await rejected(client, 'browser_navigate', url=outside_file.as_uri()))
            check('tabs_open_cannot_bypass_file_boundary', await rejected(client, 'browser_tabs', action='open', url=outside_file.as_uri()))
            check('file_url_symlink_rejected', await rejected(client, 'browser_navigate', url=linked_file.as_uri()))
            check('file_url_hardlink_rejected', await rejected(client, 'browser_navigate', url=hardlinked_file.as_uri()))
            check('file_url_hidden_symlink_rejected', await rejected(client, 'browser_navigate', url=(blocked_dir / 'alias.txt').as_uri()))
            check('file_url_embedded_outside_frame_rejected', await rejected(client, 'browser_navigate', url=frame_page.as_uri()))
            check('embedded_file_blocks_screenshot', await rejected(client, 'browser_screenshot'))
            check('embedded_file_blocks_evaluate', await rejected(client, 'browser_evaluate', expression='document.title'))
            frame_tabs = await data(client, 'browser_tabs', action='list')
            check('restricted_tab_title_not_returned', frame_tabs['tabs'][0].get('access_denied') is True and frame_tabs['tabs'][0]['url'] == '' and 'Allowed container' not in json.dumps(frame_tabs))
            await data(client, 'browser_navigate', url=link_page.as_uri())
            check('file_link_navigation_result_rejected', await rejected(client, 'browser_click', text='Outside fixture'))
            check('already_open_outside_file_blocks_snapshot', await rejected(client, 'browser_snapshot', mode='text'))
            check('already_open_outside_file_blocks_select', await rejected(client, 'browser_tabs', action='select', tab_id='t1'))
            outside_tabs = await data(client, 'browser_tabs', action='list')
            check('outside_file_title_not_returned', outside_tabs['tabs'][0].get('access_denied') is True and 'PRIVATE-TITLE-FIXTURE' not in json.dumps(outside_tabs))
            opened = await data(client, 'browser_navigate', url=site + '/')
            check('navigate_away_from_restricted_page_recovers', opened['title'] == 'LocalPilot Browser Fixture')
            snap = opened['snapshot']
            check('shadow_dom_and_select_visible', '影子按钮' in snap and 'options=生产|测试' in snap and 'checkbox' in snap)
            q_ref = ref_of(snap, 'textbox "搜索应用"')
            typed = await data(client, 'browser_type', ref=q_ref, text='com.example.demo', submit=True)
            check('type_and_submit_navigates', 'q=com.example.demo' in typed['url'] and typed['submitted'] and 'Results' in typed['snapshot'])
            check('stale_ref_after_navigation_rejected', await rejected(client, 'browser_click', ref=q_ref))
            home = await data(client, 'browser_click', text='Home')
            check('click_by_text_returns_home', home['title'] == 'LocalPilot Browser Fixture')
            snap = home['snapshot']
            selected = await data(client, 'browser_act', action='select_option', ref=ref_of(snap, 'combobox "环境"'), value='测试')
            checked = await data(client, 'browser_act', action='check', ref=ref_of(snap, 'checkbox'))
            check('select_and_check_reflected_in_snapshot', 'value="test"' in selected['snapshot'] and 'checked' in checked['snapshot'])
            shadow = await data(client, 'browser_click', ref=ref_of(snap, '影子按钮'))
            check('shadow_dom_button_clickable', shadow['title'] == 'Shadow Clicked')
            text = await data(client, 'browser_snapshot', mode='text')
            check('text_mode_reads_page', '这是一段正文' in text['text'] and text['total_chars'] > 10)
            full = await data(client, 'browser_snapshot', mode='full')
            check('full_mode_includes_headings_and_text', '# Fixture Home' in full['snapshot'] and '这是一段正文' in full['snapshot'])
            second = await data(client, 'browser_click', text='第二页')
            scrolled = await data(client, 'browser_act', action='scroll', selector='#bottom')
            waited = await data(client, 'browser_act', action='wait_for', text='底部')
            back = await data(client, 'browser_act', action='back')
            # The home page was restored from bfcache with the title the shadow button set earlier.
            check('scroll_wait_and_back', second['title'] == 'Second Page' and scrolled['action'] == 'scroll' and 'waited_for' in waited and back['url'].rstrip('/') == site and back['title'] in ('LocalPilot Browser Fixture', 'Shadow Clicked'))
            evaluated = await data(client, 'browser_evaluate', expression='({count: document.querySelectorAll("a").length, title: document.title})')
            check('evaluate_returns_json', evaluated['result']['count'] >= 1 and evaluated['result']['title'] in ('LocalPilot Browser Fixture', 'Shadow Clicked'))
            shot = await call(client, 'browser_screenshot')
            kinds = [c.type for c in shot.content]
            check('screenshot_returns_image_and_file', 'image' in kinds and Path(shot.structured_content['path']).is_file() and 'images' not in shot.structured_content and shot.structured_content['width'] > 100 and 'url' not in shot.structured_content and shot.structured_content['page_url'].startswith(site))
            import hashlib
            image_block = next(c for c in shot.content if c.type == 'image')
            image_bytes = base64.b64decode(image_block.data)
            with __import__('PIL.Image', fromlist=['open']).open(io.BytesIO(image_bytes)) as decoded:
                check('screenshot_receipt_matches_actual_delivered_pixels', shot.structured_content['image_sha256'] == hashlib.sha256(image_bytes).hexdigest()
                      and len(image_bytes) == shot.structured_content['image_bytes'] <= 1048576 and decoded.size == (shot.structured_content['width'],shot.structured_content['height']))
            element_shot = await call(client, 'browser_screenshot', selector='h1')
            check('element_screenshot_works', 'image' in [c.type for c in element_shot.content] and element_shot.structured_content['original_height'] < 300)
            tabs = await data(client, 'browser_tabs', action='open', url=site + '/second')
            check('tabs_open_and_list', len(tabs['tabs']) == 2 and tabs['active'] == 't2' and any(t['title'] == 'Second Page' for t in tabs['tabs']))
            closed = await data(client, 'browser_tabs', action='close', tab_id='t2')
            check('tabs_close', len(closed['tabs']) == 1 and closed['active'] == 't1')
            # dialogs
            await data(client, 'browser_navigate', url=site + '/')
            kept = await data(client, 'browser_click', text='删除')
            check('confirm_dismissed_by_default_and_reported', kept['title'] == 'Kept' and kept['dialogs'][0]['type'] == 'confirm' and kept['dialogs'][0]['action'] == 'dismissed' and 'hint' in kept['dialogs'][0])
            deleted = await data(client, 'browser_click', text='删除', dialog='accept')
            check('confirm_accepted_on_request', deleted['title'] == 'Deleted' and deleted['dialogs'][0]['action'] == 'accepted' and '确定删除' in deleted['dialogs'][0]['message'])
            named = await data(client, 'browser_click', text='询问', dialog='accept', dialog_text='小明')
            check('prompt_receives_text', named['title'] == 'Name:小明' and named['dialogs'][0]['type'] == 'prompt')
            alerted = await data(client, 'browser_click', text='提示')
            check('alert_auto_accepted_and_reported', alerted['title'] == 'Alerted' and alerted['dialogs'][0]['type'] == 'alert' and alerted['dialogs'][0]['action'] == 'accepted')
            # refs are bound to element content, not only to DOM nodes
            rows = await data(client, 'browser_snapshot')
            row_a = ref_of(rows['snapshot'], 'Row A')
            await data(client, 'browser_click', text='换行', snapshot=False)
            failure = await error_text(client, 'browser_click', ref=row_a)
            check('changed_element_ref_rejected', failure is not None and 'Row A' in failure and 'Row X' in failure)
            await data(client, 'browser_snapshot')
            moved = await data(client, 'browser_click', ref=row_a)
            check('ref_valid_again_after_new_snapshot', moved['title'] == 'clicked Row X')
            # full-page tiles, unique files, load timeouts
            await data(client, 'browser_navigate', url=site + '/tall')
            tall = await call(client, 'browser_screenshot', full_page=True, max_tiles=2)
            body = tall.structured_content
            check('full_page_screenshot_tiled', body['tiles'] == 2 and body['total_tiles'] >= 3 and len([c for c in tall.content if c.type == 'image']) == 2 and body['tile_sizes'][0][0] >= 1000 and 'note' in body)
            first = await call(client, 'browser_screenshot'); again = await call(client, 'browser_screenshot')
            check('screenshot_files_never_collide', first.structured_content['path'] != again.structured_content['path'] and Path(again.structured_content['path']).is_file())
            slow = await data(client, 'browser_navigate', url=site + '/slow', timeout_ms=2000)
            check('load_timeout_still_returns_snapshot', slow['load_timed_out'] is True and slow['title'] == 'Slow' and '可用按钮' in slow['snapshot'] and 'note' in slow)
            check('unreachable_server_still_errors', await rejected(client, 'browser_navigate', url=f'http://127.0.0.1:{free_port()}/', timeout_ms=3000))
            # the user's-Chrome detector must not count LocalPilot's own Chrome (running now on the private profile)
            sys.path.insert(0, str(source / 'agent'))
            from browser_control import BrowserControl
            def control(user_dir):
                cfg = {**browser_cfg, 'cdp_url': None, 'chrome_path': None, 'launch_if_missing': False, 'snapshot_max_chars': 12000, 'screenshot_max_side': 1600, 'user_chrome_dir': str(user_dir)}
                return BrowserControl({'state_dir': str(state), 'browser': cfg}, None)
            protected = base / 'protected'; protected.mkdir()
            guarded_control = BrowserControl({'state_dir': str(state), 'browser': {**browser_cfg, 'cdp_url': None, 'chrome_path': None, 'launch_if_missing': False,
                                             'snapshot_max_chars': 12000, 'screenshot_max_side': 1600, 'user_chrome_dir': str(user_chrome)},
                                              'permission_mode': 'full_machine', 'workspaces': {'machine': '/'}, 'protected_paths': [str(protected)]}, None)
            try:
                guarded_control._authorize_url((protected / 'secret.txt').as_uri())
                protected_denied = False
            except ToolError as exc:
                protected_denied = '受保护目录' in str(exc)
            check('full_machine_file_url_still_denies_protected_paths', protected_denied)
            check('user_chrome_detection_matches_data_dir', control(profile_dir)._user_chrome_running() is True and control(base / 'nowhere')._user_chrome_running() is False)
            fresh = control(user_chrome)
            check('fresh_agent_restores_managed_dynamic_endpoint', fresh.browser is None and fresh._probe() is not None and fresh.cdp_url != site)
            try:
                fresh.clone_logins(force=True)
                guarded = False
            except Exception as exc:
                guarded = '目标' in str(exc)
            check('fresh_agent_rejects_clone_into_live_real_Chrome', guarded)
            if fresh.loop:
                fresh.loop.call_soon_threadsafe(fresh.loop.stop)
            # task integration
            task = await data(client, 'create_task', workspace='project', objective='Check the fixture site through the browser.',
                              plan=[{'id': 'browse', 'step': 'Look at the page', 'status': 'in_progress'}],
                              checks=[{'kind': 'file_equals', 'path': 'note.txt', 'value': 'done\n'}])
            await data(client, 'browser_navigate', url=site + '/')
            observed = await call(client, 'inspect_task_step', task_id=task['task_id'], step_id='browse', operation='browser_snapshot', arguments={'mode': 'interactive'}, action_id='snap')
            step_shot = await call(client, 'inspect_task_step', task_id=task['task_id'], step_id='browse', operation='browser_screenshot', arguments={}, action_id='shot')
            acted = await call(client, 'run_task_step', task_id=task['task_id'], step_id='browse', operation='browser_click', arguments={'text': '第二页'}, action_id='go-second')
            check('task_browser_observation_and_mutation', observed.structured_content['task']['action_count'] == 0 and observed.structured_content['task']['observation_count'] == 1
                  and 'image' in [c.type for c in step_shot.content] and acted.structured_content['task']['action_count'] == 1 and acted.structured_content['action']['result']['title'] == 'Second Page')
            replay = await call(client, 'inspect_task_step', task_id=task['task_id'], step_id='browse', operation='browser_screenshot', arguments={}, action_id='shot')
            check('real_screenshot_replay_after_navigation_keeps_original_pixels', replay.structured_content['replayed'] and
                  [c.data for c in replay.content if c.type == 'image'] == [c.data for c in step_shot.content if c.type == 'image'])
            waiting = asyncio.create_task(call(client, 'run_task_step', task_id=task['task_id'], step_id='browse', operation='browser_act', arguments={'action':'wait_for','value':10000,'snapshot':False}, action_id='wait'))
            await asyncio.sleep(0.3)
            await data(client, 'set_task_state', task_id=task['task_id'], status='paused', reason='Browser cancellation regression')
            cancelled = (await waiting).structured_content['action']
            check('real_browser_call_cancelled_by_harness_pause', cancelled['status'] == 'interrupted' and cancelled['result']['cancel_requested'] and cancelled['result']['call_settled'])
            with sqlite3.connect(state / 'localpilot.sqlite3') as db:
                stored = db.execute("SELECT snapshot FROM task_actions WHERE action_id='shot'").fetchone()[0]
                events = [row[0] for row in db.execute("SELECT tool FROM events WHERE tool LIKE 'browser_%'")]
            check('screenshot_base64_not_persisted_in_receipts', 'images' not in json.loads(stored)['result'] and json.loads(stored)['result']['image_count'] == 1 and len(stored) < 20000)
            check('browser_receipts_recorded', 'browser_navigate' in events and 'browser_click' in events and 'browser_screenshot' in events)
            # MCP bridge image passthrough
            picture = await call(client, 'call_mcp_tool', server='pictures', tool='picture', arguments={'size': 48})
            check('bridge_passes_images_to_model', 'image' in [c.type for c in picture.content] and picture.structured_content['image_count'] == 1 and '[image 1' in picture.structured_content['text'] and 'images' not in picture.structured_content)
            check('clone_logins_refused_while_connected', await rejected(client, 'browser_tabs', action='clone_logins'))
            stopped = await data(client, 'browser_tabs', action='stop')
            check('stop_closes_localpilot_chrome', stopped['stopped_localpilot_chrome'] is True and (await data(client, 'browser_tabs', action='status'))['connected'] is False)
            # login cloning from the user's Chrome (fake layout; the real one needs the user's Chrome closed)
            cloned = await data(client, 'browser_tabs', action='clone_logins')
            check('clone_logins_uses_last_used_profile', cloned['source_profile']['dir'] == 'Profile 1' and cloned['source_profile']['name'] == '主号' and 'Cookies' in cloned['copied']
                  and 'Local Storage' in cloned['copied'] and cookie_host(profile_dir) == 'main.example' and not cloned['errors'])
            local_state = (profile_dir / 'Local State').read_text('utf-8') if (profile_dir / 'Local State').is_file() else ''  # Chrome writes its own; the user's must not be copied
            check('clone_logins_skips_local_state_and_copies_local_storage', '旧号' not in local_state and 'Profile 1' not in local_state
                  and (profile_dir / 'Default/Local Storage/leveldb/000001.ldb').is_file())
            other = await data(client, 'browser_tabs', action='clone_logins', profile='旧号')
            check('clone_logins_profile_by_display_name', other['source_profile']['dir'] == 'Default' and cookie_host(profile_dir) == 'old.example')
            check('clone_logins_unknown_profile_rejected', await rejected(client, 'browser_tabs', action='clone_logins', profile='Nope'))
            status = await data(client, 'browser_tabs', action='status')
            check('status_lists_user_profiles', [p['dir'] for p in status['user_profiles']][:2] == ['Profile 1', 'Default'] and status['user_profiles'][0]['last_used'] is True and 'mtime' not in status['user_profiles'][0])
        disabled = json.loads(config.read_text()); disabled['browser']['enabled'] = False; config.write_text(json.dumps(disabled))
        async with Client(params) as client:
            check('browser_disabled_by_config', await rejected(client, 'browser_navigate', url=site + '/'))
    server.shutdown()
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'scope': 'Real local MCP; headless Chrome on a private profile; local fixture site; fake user-Chrome profile layout; no ChatGPT claims',
              'checks': checks, 'passed': sum(c['passed'] for c in checks), 'total': len(checks)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'total': report['total'], 'report': str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'verification/v0.7.1-browser.json')
    asyncio.run(verify(parser.parse_args().output))
