"""Drive the task panel in a real Chrome page with a simulated MCP Apps host; also renders screenshots.

Runs with the system Python that has playwright installed (python3 -m pip install playwright) and the
installed Google Chrome. Browser verification inside ChatGPT is separate and is not claimed here.
"""
from __future__ import annotations
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PANEL = (ROOT / 'agent/task_panel.html').read_text(encoding='utf-8')
# The test copy polls every 250 ms and treats a follow-up as unacknowledged after 1.5 s (production: 5 s / 20 s).
assert PANEL.count('}, 5000);') == 1 and PANEL.count('}, 20000);') == 1
FAST_PANEL = PANEL.replace('}, 5000);', '}, 250);').replace('}, 20000);', '}, 1500);')
FAST_PANEL = FAST_PANEL.replace('const TASK_DATA_TIMEOUT_MS = 10000;', 'const TASK_DATA_TIMEOUT_MS = 400;')
RECEIPT = (ROOT / 'agent/task_step_receipt.html').read_text(encoding='utf-8')
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'verification/panel-r11'
REPORT = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / 'verification/panel-latest.json'
NOW = 1_788_939_400.0
TRACE = ('Traceback (most recent call last):\n  File "/Users/me/project/selftest/chat-harness-1788935922/verify.py", line 2, in <module>\n'
         '    assert add(2, 3) == 5\nAssertionError\n')
HOST = """<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:__BG__">
<iframe id="f" sandbox="allow-scripts" style="border:0;width:100%;height:200px;display:block"></iframe>
<iframe id="sibling" sandbox="allow-scripts" style="display:none"></iframe>
<script>
window.__log = []; window.__claims = 0;
window.__opts = {ack:true, textOnly:false, failGetTask:0, hostContext:{theme:'light',displayMode:'inline',availableDisplayModes:['inline','fullscreen']}};
const f = document.getElementById('f');
function reply(msg, result, error) {f.contentWindow.postMessage(error ? {jsonrpc:'2.0',id:msg.id,error} : {jsonrpc:'2.0',id:msg.id,result}, '*');}
window.__send = (method, params, id) => {const m = {jsonrpc:'2.0',method,params}; if (id !== undefined) m.id = id; f.contentWindow.postMessage(m, '*');};
window.addEventListener('message', ev => {
  if (ev.source !== f.contentWindow) return;
  const msg = ev.data; if (msg?.jsonrpc !== '2.0') return; window.__log.push(msg);
  if (msg.method === 'ui/initialize') {
    reply(msg, {protocolVersion:'2026-01-26',hostInfo:{name:'test-host'},hostContext:window.__opts.hostContext});
    // Standard flow: the host pushes the triggering tool call's input and result after the handshake.
    if (window.__opts.notifyResult !== false && window.__task) {window.__send('ui/notifications/tool-input', {arguments:{task_id:window.__task.task_id}}); window.__send('ui/notifications/tool-result', {structuredContent:window.__task});}
  }
  if (msg.method === 'tools/call') {
    const name = msg.params.name, args = msg.params.arguments; let value;
    if (name === 'get_task') {
      if (window.__opts.failGetTask > 0) {window.__opts.failGetTask--; return reply(msg, null, {code:-32000,message:'simulated transport failure'});}
      if (window.__opts.finishOnGet) {
        window.__opts.finishOnGet = false;
        window.__task.checks_status = {passed:1,total:1,checks:[{passed:true,evidence:{status:'completed',exit_code:0,action_id:'verify'}}],running_jobs:[]};
        window.__activity.records[0] = {...window.__activity.records[0],outcome:'completed',duration_seconds:8,result:{...window.__activity.records[0].result,status:'completed',exit_code:0,finished_at:1008,stdout:'FINAL OUTPUT',preview_truncated:false}};
      }
      value = window.__task;
    } else if (name === 'get_task_activity') {
      value = args.action_id ? {task_id:window.__task.task_id,total:window.__activity.total,records:window.__activity.records.filter(r => r.action_id === args.action_id).map(r => ({...r,result:{...r.result,stdout:(r.result.stdout || '') + '\\n[FULL OUTPUT LOADED]',preview_truncated:false}}))} : window.__activity;
    } else if (name === 'claim_task_continuation') {
      window.__claims += 1; window.__task = {...window.__task,continuations:window.__claims,last_continued_revision:args.expected_revision};
      value = {send:true,sequence:window.__claims,prompt:'继续现有测试任务'};
    } else if (name === 'set_task_state') {
      window.__task = {...window.__task,status:args.status,effective_status:args.status,revision:window.__task.revision + 1,pause_reason:args.reason}; value = window.__task;
    } else value = {};
    reply(msg, window.__opts.textOnly ? {content:[{type:'text',text:JSON.stringify(value)}]} : {structuredContent:value});
  }
  if (msg.method === 'ui/message' && window.__opts.ack) reply(msg, {});
  if (msg.method === 'ui/request-display-mode') {reply(msg, {mode:msg.params.mode}); window.__send('ui/notifications/host-context-changed', {displayMode:msg.params.mode});}
  if (msg.method === 'ui/notifications/size-changed') f.style.height = msg.params.height + 'px';
});
window.__mount = html => {f.srcdoc = html;};
window.__evil = () => {document.getElementById('sibling').srcdoc = "<script>parent.frames[0].postMessage({jsonrpc:'2.0',method:'ui/notifications/tool-result',params:{structuredContent:{task_id:'evil',objective:'bad',plan:[],checks:[],revision:999}}},'*')<\\/script>";};
</script></body></html>"""


def plan(status_index):
    steps = ['调用 finish_task 确认提前完成会被拒绝。', '运行原始 python3 verify.py，记录失败原因与退出码。',
             '用实读 SHA-256 保护单处替换，把 calculator.py 的 return a - b 改为 return a + b；不修改 verify.py。',
             '重新运行 verify:2，从真实输出取得 HARNESS_VERIFIED 标记。', '复读两文件并核对回执，提交 review_task 与 finish_task。']
    ids = ['reject-premature-finish', 'baseline', 'repair', 'verify-fixed', 'audit-finish']
    return [{'id': i, 'step': s, 'status': 'completed' if n < status_index else 'in_progress' if n == status_index else 'pending'}
            for n, (i, s) in enumerate(zip(ids, steps))]


CHECKS = [{'kind': 'file_sha256', 'path': 'selftest/chat-harness-1788935922/verify.py', 'value': '78bb72e18e2f9b119a270fb7cfe5068c1e92b0ffa3db47f425f1df07b1fd5cf0', 'action_id': None},
          {'kind': 'file_contains', 'path': 'selftest/chat-harness-1788935922/calculator.py', 'value': 'return a + b', 'action_id': None},
          {'kind': 'job_succeeded', 'path': None, 'value': None, 'action_id': 'verify'}]


def job(job_id, status, exit_code, stdout='', stderr='', started=NOW - 130):
    return {'job_id': job_id, 'status': status, 'exit_code': exit_code, 'cwd': '/Users/me/project/selftest/chat-harness-1788935922',
            'stdout': stdout, 'stderr': stderr, 'output_truncated': False, 'timeout_seconds': 30, 'network_enabled': False,
            'started_at': started, 'finished_at': started + 0.08, 'receipt_id': '98d0d72131a241f894d43dbec5b80bb6'}


def record(action_id, step_id, operation, status, started, result, inputs, outcome=None, duration=0.08):
    return {'action_id': action_id, 'step_id': step_id, 'operation': operation, 'status': status, 'started_at': started,
            'finished_at': started + duration, 'inputs': inputs, 'result': result, 'outcome': outcome or status, 'duration_seconds': duration}


def fixtures(stage):
    """stage 'active' = mid-task after a failed verification; 'complete' = fully verified."""
    base = {'task_id': '648d31147f2547f9b919a14ed4a36a88', 'workspace': 'project', 'device_label': 'Demo Mac',
            'objective': '在普通 Chat 中修复 selftest/chat-harness-1788935922/calculator.py 的 add，使原始 verify.py 全部断言通过，并从真实运行结果取得验证标记。仅可读取这两个文件、修改 calculator.py、在该目录执行 python3 verify.py；文件与测试操作通过 run_task_step，不使用 Work、Codex 或额外模型 API。',
            'checks': CHECKS, 'created_at': NOW - 800, 'deadline': NOW + 2800, 'max_actions': 40, 'max_continuations': 3,
            'last_decision': '原测试失败，准备修复 calculator.py。', 'pause_reason': None, 'features': {'step_cards': False}}
    verify_fail = record('verify', 'baseline', 'run_shell', 'succeeded', NOW - 130, job('b6ce21709c71472fba9e9b80959bd31d', 'failed', 1, '', TRACE), {'command': 'python3 verify.py', 'cwd': 'selftest/chat-harness-1788935922', 'timeout_seconds': 30}, 'failed')
    status_1 = record('baseline-status', 'baseline', 'job_status', 'succeeded', NOW - 122, job('b6ce21709c71472fba9e9b80959bd31d', 'failed', 1, '', TRACE), {'job_id': 'b6ce21709c71472fba9e9b80959bd31d'}, 'failed', 0.001)
    read_1 = record('read-calculator-before-repair', 'repair', 'read_file', 'succeeded', NOW - 95, {'path': '/Users/me/project/selftest/chat-harness-1788935922/calculator.py', 'sha256': '0b1c9d7e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c', 'bytes': 32, 'total_chars': 32, 'next_offset': None, 'receipt_id': 'dcc8f4ad34bd459eac8d53d6dd5d8886'}, {'path': 'selftest/chat-harness-1788935922/calculator.py'}, 'succeeded', 0.004)
    if stage == 'active':
        task = {**base, 'plan': plan(2), 'status': 'active', 'effective_status': 'active', 'revision': 9, 'updated_at': NOW - 95,
                'action_count': 3, 'continuations': 1, 'last_continued_revision': 3, 'last_mutation_at': NOW - 130,
                'idle_seconds': 95.0, 'elapsed_seconds': 800, 'remaining_seconds': 2800,
                'step_stats': {'baseline': {'actions': 2, 'failed': 2, 'last': {'action_id': 'baseline-status', 'operation': 'job_status', 'outcome': 'failed', 'exit_code': 1, 'finished_at': NOW - 122}},
                               'repair': {'actions': 1, 'failed': 0, 'last': {'action_id': 'read-calculator-before-repair', 'operation': 'read_file', 'outcome': 'succeeded', 'exit_code': None, 'finished_at': NOW - 95}}},
                'checks_status': {'decision': 'continue', 'running_jobs': [], 'passed': 1, 'total': 3, 'checks': [
                    {'passed': True, 'evidence': {'path': '/Users/me/project/selftest/chat-harness-1788935922/verify.py', 'sha256': CHECKS[0]['value'], 'receipt_id': None}},
                    {'passed': False, 'evidence': {'path': '/Users/me/project/selftest/chat-harness-1788935922/calculator.py', 'sha256': '0b1c9d7e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c', 'receipt_id': None}},
                    {'passed': False, 'evidence': {'job_id': 'b6ce21709c71472fba9e9b80959bd31d', 'status': 'failed', 'exit_code': 1, 'receipt_id': '98d0d72131a241f894d43dbec5b80bb6', 'action_id': 'verify'}}]}}
        activity = {'task_id': task['task_id'], 'total': 3, 'records': [read_1, status_1, verify_fail], 'last_decision': task['last_decision']}
        return task, activity
    repair = record('repair-add', 'repair', 'replace_text', 'succeeded', NOW - 74, {'path': '/Users/me/project/selftest/chat-harness-1788935922/calculator.py', 'status': 'updated', 'previous_sha256': '0b1c9d7e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c', 'sha256': 'ba1a531f581d2e6094e978ed6f7aca7a8d92eeb62c6e7ad73ee692f7f18bc772', 'bytes': 32, 'receipt_id': 'bbd3a9b6ed374c8aa75c0ef321282f34'}, {'path': 'selftest/chat-harness-1788935922/calculator.py', 'expected_sha256': '0b1c9d7e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c', 'old_text': '    return a - b\n', 'new_text': '    return a + b\n', 'old_text_chars': 17, 'new_text_chars': 17}, 'succeeded', 0.006)
    verify_ok = record('verify:2', 'verify-fixed', 'run_shell', 'succeeded', NOW - 60, job('eaff3e7f9073480589597801336d1785', 'completed', 0, 'HARNESS_VERIFIED::f40ba2231dfdd39bc4d4a491\n', '', NOW - 60), {'command': 'python3 verify.py', 'cwd': 'selftest/chat-harness-1788935922', 'timeout_seconds': 30}, 'completed', 0.09)
    long_out = record('list-dir', 'audit-finish', 'run_shell', 'succeeded', NOW - 40, {**job('c0ffee0073480589597801336d1785aa', 'completed', 0, ('line of output\n' * 300), '', NOW - 40), 'output_truncated': False, 'stdout': ('line of output\n' * 200)[:3000], 'preview_truncated': True}, {'command': 'ls -la && cat calculator.py', 'cwd': 'selftest/chat-harness-1788935922', 'timeout_seconds': 30}, 'completed', 0.05)
    task = {**base, 'plan': plan(5), 'status': 'complete', 'effective_status': 'complete', 'revision': 25, 'updated_at': NOW - 20,
            'action_count': 8, 'continuations': 2, 'last_continued_revision': 3, 'last_mutation_at': NOW - 40,
            'idle_seconds': 20.0, 'elapsed_seconds': 780, 'remaining_seconds': 2820, 'last_decision': '证据核对完成，提交 review_task 和 finish_task 验收。',
            'step_stats': {'baseline': {'actions': 2, 'failed': 2, 'last': {'action_id': 'baseline-status', 'operation': 'job_status', 'outcome': 'failed', 'exit_code': 1, 'finished_at': NOW - 122}},
                           'repair': {'actions': 2, 'failed': 0, 'last': {'action_id': 'repair-add', 'operation': 'replace_text', 'outcome': 'succeeded', 'exit_code': None, 'finished_at': NOW - 74}},
                           'verify-fixed': {'actions': 1, 'failed': 0, 'last': {'action_id': 'verify:2', 'operation': 'run_shell', 'outcome': 'completed', 'exit_code': 0, 'finished_at': NOW - 60}},
                           'audit-finish': {'actions': 1, 'failed': 0, 'last': {'action_id': 'list-dir', 'operation': 'run_shell', 'outcome': 'completed', 'exit_code': 0, 'finished_at': NOW - 40}}},
            'checks_status': {'decision': 'complete', 'running_jobs': [], 'passed': 3, 'total': 3, 'checks': [
                {'passed': True, 'evidence': {'path': '/Users/me/project/selftest/chat-harness-1788935922/verify.py', 'sha256': CHECKS[0]['value'], 'receipt_id': None}},
                {'passed': True, 'evidence': {'path': '/Users/me/project/selftest/chat-harness-1788935922/calculator.py', 'sha256': 'ba1a531f581d2e6094e978ed6f7aca7a8d92eeb62c6e7ad73ee692f7f18bc772', 'receipt_id': None}},
                {'passed': True, 'evidence': {'job_id': 'eaff3e7f9073480589597801336d1785', 'status': 'completed', 'exit_code': 0, 'receipt_id': '12947493e8af427298ad37bd7b1639a8', 'action_id': 'verify:2'}}]}}
    activity = {'task_id': task['task_id'], 'total': 8, 'records': [long_out, verify_ok, repair, read_1, status_1, verify_fail], 'last_decision': task['last_decision']}
    return task, activity


class Panel:
    def __init__(self, browser, width, task, activity, opts=None, dark=False, preload=True):
        self.page = browser.new_page(viewport={'width': width, 'height': 900}, color_scheme='dark' if dark else 'light')
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.on('console', lambda message: self.errors.append(f'{message.type}: {message.text}') if message.type == 'error' else None)
        Panel.current = self
        self.page.set_content(HOST.replace('__BG__', '#111' if dark else '#fff'))
        self.page.evaluate('([t,a,o]) => {window.__task=t; window.__activity=a; Object.assign(window.__opts,o||{});}', [task, activity, {**(opts or {}), 'notifyResult': preload}])
        self.page.evaluate('html => window.__mount(html)', FAST_PANEL)
        self.settle(600)
        self.frame = self.page.query_selector('#f').content_frame()

    def settle(self, ms=250):
        self.page.wait_for_timeout(ms)

    def tick(self, count=1):
        # One poll interval of the test copy plus a margin for the tool round trip.
        self.page.wait_for_timeout(350 * count)

    def text(self, selector):
        return self.frame.locator(selector).first.text_content() or ''

    def open_timeline(self):
        # The 0.6 panel opens the execution log by default; only click when it is closed.
        if not self.frame.evaluate("() => document.getElementById('timeline').open"):
            self.frame.locator('#timeline > summary').click()
        self.settle(200)

    def heights(self):
        """(host iframe height from size-changed, actual panel document height)."""
        self.settle(300)
        return (self.page.evaluate("() => parseInt(document.getElementById('f').style.height)"), self.frame.evaluate('() => document.documentElement.scrollHeight'))

    def shot(self, name):
        # Chrome only rasterises iframe content inside the viewport, so size the viewport to the content first.
        hosted, actual = self.heights()
        self.page.set_viewport_size({'width': self.page.viewport_size['width'], 'height': max(actual, hosted) + 4})
        self.frame.evaluate('() => window.scrollTo(0, 0)'); self.settle(300)
        self.page.screenshot(path=str(SHOTS / name), full_page=False)

    def log(self, method):
        return [m for m in self.page.evaluate('() => window.__log') if m.get('method') == method]

    def close(self):
        self.page.close()


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    checks = []
    def record_check(name, passed):
        checks.append({'name': name, 'passed': bool(passed)}); print(('PASS ' if passed else 'FAIL '), name)
        if not passed and getattr(Panel, 'current', None):
            panel = Panel.current
            print('   errors:', panel.errors[-3:])
            print('   feedback:', panel.text('#feedback'), '| records:', (panel.frame.evaluate("() => document.getElementById('records').innerText") or '')[:200].replace('\n', ' '))
    active_task, active_activity = fixtures('active')
    complete_task, complete_activity = fixtures('complete')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel='chrome')
        # 1. Standard bridge connects, renders the live task, and the continue button becomes usable.
        panel = Panel(browser, 720, active_task, active_activity)
        record_check('bridge_waits_for_live_task_before_enabling', not panel.frame.locator('#next').is_disabled() and panel.text('#feedback') == '任务进度已同步。')
        record_check('full_objective_with_expand_toggle', panel.text('#objective') == active_task['objective'] and panel.frame.locator('#objective-toggle').is_visible())
        record_check('stats_and_segments_reflect_task', panel.text('#stat-steps').startswith('2 / 5') and panel.text('#stat-checks').startswith('1 / 3') and panel.frame.locator('#segments i').count() == 5)
        record_check('checks_show_live_pass_fail_and_evidence', panel.frame.locator('#checks .glyph[data-s=pass]').count() == 1 and panel.frame.locator('#checks .glyph[data-s=fail]').count() == 2 and '退出码 1' in panel.text('#checks'))
        record_check('plan_shows_per_step_action_stats', '2 个动作 · 2 个失败' in panel.text('#plan') and panel.frame.locator('#plan .step[data-s=in_progress]').count() == 1)
        record_check('size_changed_notifications_sent', len(panel.log('ui/notifications/size-changed')) >= 1 and panel.page.evaluate("() => parseInt(document.getElementById('f').style.height) > 300"))
        record_check('no_horizontal_overflow', panel.frame.evaluate('() => document.documentElement.scrollWidth <= document.documentElement.clientWidth'))
        panel.open_timeline(); panel.frame.locator('#records .rec').first.wait_for(timeout=5000); panel.settle(200)
        groups = panel.frame.locator('#records .group')
        record_check('timeline_grouped_by_plan_step_in_order', groups.count() == 2 and '运行原始' in (groups.nth(0).text_content() or '') and panel.frame.locator('#records .rec').count() == 3)
        panel.frame.locator('#records .rec').first.locator('summary').click(); panel.settle(200)
        record_check('record_expands_to_command_output_and_ids', 'AssertionError' in panel.text('#records') and 'python3 verify.py' in panel.text('#records') and 'job b6ce2170' in panel.text('#records'))
        hosted, actual = panel.heights()
        record_check('host_iframe_height_tracks_reported_size', abs(hosted - actual) <= 2)
        panel.shot('inline-active-light.png')
        panel.open_timeline(); panel.settle(300)
        record_check('collapsed_panel_reports_natural_height', abs(panel.heights()[0] - panel.frame.locator('.panel').bounding_box()['height']) <= 2)
        panel.frame.locator('#plan .step').nth(2).click(); panel.settle(300)
        record_check('clicking_step_filters_timeline', not panel.frame.locator('#filter').is_hidden() and panel.frame.locator('#records .rec').count() == 1 and '读取文件' in panel.text('#records'))
        panel.shot('inline-filter-light.png')
        panel.frame.locator('#filter-clear').click(); panel.settle(200)
        record_check('filter_clears', panel.frame.locator('#records .rec').count() == 3)
        panel.frame.locator('#next').click(); panel.settle(400)
        messages = panel.log('ui/message')
        record_check('followup_uses_live_host_array_schema', len(messages) == 1 and isinstance(messages[0]['params']['content'], list) and messages[0]['params']['content'][0]['type'] == 'text')
        record_check('receipt_prevents_duplicate_button_send', panel.frame.locator('#next').is_disabled() and panel.text('#next') == '已请求继续')
        # Idle uses the agent clock: a stale updated_at but fresh idle_seconds must not trigger automatic continuation.
        panel.page.evaluate('() => {window.__task = {...window.__task, revision: 10, updated_at: 1000, idle_seconds: 3};}')
        panel.tick(); panel.frame.locator('#auto').click(); panel.settle(100); panel.tick(); panel.tick()
        record_check('idle_uses_agent_clock_not_browser_clock', len(panel.log('ui/message')) == 1)
        panel.page.evaluate('() => {window.__task = {...window.__task, idle_seconds: 120};}')
        panel.tick(); panel.tick()
        record_check('automatic_followup_once_per_revision', len(panel.log('ui/message')) == 2)
        panel.frame.locator('#mode').click(); panel.settle(300)
        record_check('fullscreen_request_and_layout', len(panel.log('ui/request-display-mode')) == 1 and panel.frame.evaluate("() => document.documentElement.dataset.mode === 'fullscreen' && document.getElementById('timeline').open"))
        panel.close()
        # 2. Fullscreen layout screenshot at desktop width with the completed task.
        panel = Panel(browser, 1240, complete_task, complete_activity, {'hostContext': {'theme': 'light', 'displayMode': 'fullscreen', 'availableDisplayModes': ['inline', 'fullscreen']}})
        panel.frame.locator('#records .rec').first.wait_for(timeout=5000); panel.settle(200)
        record_check('completed_task_renders_verified_state', panel.text('#status') == '已验收' and panel.frame.locator('#checks .glyph[data-s=pass]').count() == 3 and '任务已通过验收' in panel.text('#note'))
        records = panel.frame.locator('#records .rec')
        for i in range(records.count()):
            # Each expansion grows the frame; wait for the host to apply the reported height before the next click.
            records.nth(i).locator('summary').click(); panel.settle(200)
        panel.settle(300)
        record_check('replace_text_receipt_shows_old_and_new', panel.frame.locator('#records pre.old').count() == 1 and 'return a + b' in (panel.frame.locator('#records pre.new').first.text_content() or ''))
        panel.shot('fullscreen-complete-light.png')
        panel.frame.locator('#records button.textbtn', has_text='加载已保存输出').first.click(); panel.settle(400)
        record_check('full_output_loaded_on_demand', '[FULL OUTPUT LOADED]' in panel.text('#records') and any(m['params']['arguments'].get('action_id') == 'list-dir' for m in panel.log('tools/call') if m['params']['name'] == 'get_task_activity'))
        panel.page.evaluate('() => {window.__task.checks_status.passed=2; window.__task.checks_status.checks[1].passed=false;}'); panel.tick()
        record_check('completed_task_distinguishes_current_check_failure', panel.text('#status') == '已完成 · 需复核' and panel.text('#checks-hint') != '全部通过' and '当前检查有变化' in panel.text('#note'))
        panel.close()
        # 3. Dark theme from host context, narrow width, and the compact inline layout.
        panel = Panel(browser, 390, complete_task, complete_activity, {'hostContext': {'theme': 'dark', 'displayMode': 'inline', 'availableDisplayModes': ['inline']}}, dark=True)
        record_check('dark_theme_applied_from_host_context', panel.frame.evaluate("() => document.documentElement.style.colorScheme === 'dark'"))
        record_check('fullscreen_button_hidden_when_unavailable', panel.frame.locator('#mode').is_hidden())
        record_check('narrow_width_has_no_overflow', panel.frame.evaluate('() => document.documentElement.scrollWidth <= document.documentElement.clientWidth'))
        panel.open_timeline(); panel.frame.locator('#records .rec').first.wait_for(timeout=5000); panel.settle(200)
        panel.shot('inline-complete-dark-narrow.png')
        panel.close()
        # 4. Text envelope (no structuredContent) still decodes.
        panel = Panel(browser, 720, active_task, active_activity, {'textOnly': True})
        record_check('text_envelope_decodes_to_task', not panel.frame.locator('#next').is_disabled())
        panel.close()
        # 5. No preloaded toolOutput; a later tool-result notification connects the panel.
        panel = Panel(browser, 720, active_task, active_activity, preload=False)
        record_check('initial_state_shows_skeleton_not_dashes', panel.frame.locator('.sk').count() >= 5 and panel.frame.locator('#next').is_disabled())
        panel.shot('inline-initial-light.png')
        panel.page.evaluate('() => window.__send("ui/notifications/tool-result", {structuredContent: window.__task})'); panel.tick()
        record_check('late_tool_input_connects_after_preload', not panel.frame.locator('#next').is_disabled())
        panel.close()
        # 6. Unacknowledged follow-up: reported as uncertain, never retried, fallback text shown.
        panel = Panel(browser, 720, active_task, active_activity, {'ack': False})
        panel.frame.locator('#next').click(); panel.settle(2200); panel.tick()
        record_check('unacknowledged_message_is_not_retried', len(panel.log('ui/message')) == 1 and not panel.frame.locator('#fallback').is_hidden())
        record_check('unacknowledged_message_is_not_reported_rejected', panel.frame.locator('#feedback').get_attribute('data-error') == 'false' and '尚未收到' in panel.text('#feedback'))
        panel.shot('inline-fallback-light.png')
        panel.close()
        # 7. A transient get_task failure slows polling but does not stop it.
        panel = Panel(browser, 720, active_task, active_activity)
        panel.page.evaluate('() => {window.__task.idle_seconds=3;}'); panel.tick()
        panel.frame.locator('#auto').click(); panel.settle(100)
        panel.page.evaluate('() => {window.__opts.failGetTask = 1;}')
        seen = set()
        for _ in range(30):  # Sample the transient error message, then the recovery, at 50 ms resolution.
            seen.add(panel.text('#feedback')); panel.page.wait_for_timeout(50)
        panel.tick(2)
        calls = [m for m in panel.log('tools/call') if m['params']['name'] == 'get_task']
        record_check('polling_recovers_after_transient_error', any(('自动重试' in t or '连接中断' in t) for t in seen) and len(calls) >= 3 and not panel.frame.locator('#next').is_disabled() and panel.frame.locator('#feedback').get_attribute('data-error') == 'false')
        record_check('read_failure_preserves_user_auto_continue_choice', panel.frame.locator('#auto').get_attribute('aria-pressed') == 'true')
        panel.frame.locator('#refresh').click(); panel.settle(300)
        record_check('refresh_button_resets_backoff', panel.text('#feedback') == '任务状态已更新。')
        # 8. Messages from a frame other than the parent are ignored.
        before = panel.text('#objective')
        panel.page.evaluate('() => window.__evil()'); panel.settle(300)
        record_check('messages_from_unrelated_frames_ignored', panel.text('#objective') == before)
        # 9. Pause through the panel calls set_task_state and re-renders as paused.
        panel.frame.locator('#pause').click(); panel.settle(400)
        print('   pause state:', repr(panel.text('#status')), repr(panel.text('#next')), repr(panel.text('#note')[:60]))
        record_check('pause_button_updates_state', panel.text('#status') == '已暂停' and '恢复' in panel.text('#next') and '已暂停' in panel.text('#note'))
        panel.shot('inline-paused-light.png')
        panel.close()
        # 10. Receipt card renders a run_task_step result on its own.
        page = browser.new_page(viewport={'width': 720, 'height': 400})
        page.set_content(HOST.replace('__BG__', '#fff'))
        step_result = {'replayed': False, 'action': complete_activity['records'][1], 'task': complete_task}
        page.evaluate('html => window.__mount(html)', RECEIPT); page.wait_for_timeout(250)
        page.evaluate('r => window.__send("ui/notifications/tool-result", {structuredContent: r})', step_result); page.wait_for_timeout(300)
        card = page.query_selector('#f').content_frame()
        card.locator('summary').click(); page.wait_for_timeout(200)
        text = card.locator('body').text_content() or ''
        record_check('receipt_card_shows_command_outcome_and_output', '运行命令' in text and 'python3 verify.py' in text and 'exit 0' in text and 'HARNESS_VERIFIED' in text and '步骤 5/5' in text)
        page.set_viewport_size({'width': 720, 'height': card.evaluate('() => document.documentElement.scrollHeight') + 4}); page.wait_for_timeout(200)
        page.screenshot(path=str(SHOTS / 'receipt-card-light.png'), full_page=False)
        card.locator('summary').click(); page.wait_for_timeout(250)
        record_check('collapsed_receipt_reports_natural_height', abs(page.evaluate("() => parseInt(document.getElementById('f').style.height)") - card.locator('.card').bounding_box()['height']) <= 2)
        step_result['action'] = {**step_result['action'], 'started_at':1000, 'finished_at':1000.5, 'result':{**step_result['action']['result'], 'started_at':1000, 'finished_at':1008}}
        page.evaluate('r => window.__send("ui/notifications/tool-result", {structuredContent:r})', step_result); page.wait_for_timeout(100)
        record_check('receipt_uses_job_duration', card.locator('#dur').text_content() == '8.0s')
        page.close()
        # 11. A job ends before the next get_task response without advancing the task revision.
        for load_full in (False, True):
            task = {**active_task, 'plan':[{'id':'one','step':'Verify command','status':'in_progress'}], 'checks':[{'kind':'job_succeeded','action_id':'verify'}],
                    'revision':3, 'checks_status':{'passed':0,'total':1,'checks':[{'passed':False,'evidence':{'status':'running','action_id':'verify'}}],'running_jobs':['test-job']}}
            action = record('verify', 'one', 'run_shell', 'succeeded', 1000,
                            {'job_id':'test-job','status':'running','started_at':1000,'finished_at':None,'exit_code':None,'stdout':'preview','preview_truncated':True},
                            {'command':'long validation','cwd':'.'}, 'running')
            action['duration_seconds'] = None
            panel = Panel(browser, 720, task, {'task_id':task['task_id'],'total':1,'records':[action]})
            panel.open_timeline(); panel.settle(100)
            if load_full:
                panel.frame.locator('.rec > summary').click(); panel.settle(100)
                panel.frame.get_by_role('button', name='加载已保存输出（本机最多保留 64 KiB）', exact=True).click(); panel.settle(100)
            panel.page.evaluate('() => {window.__opts.finishOnGet=true;}'); panel.tick(3)
            record_check('cached_running_output_replaced_on_completion' if load_full else 'terminal_record_refreshes_without_revision_change',
                         'exit 0' in panel.text('#records') and 'FINAL OUTPUT' in panel.text('#records'))
            panel.close()
        browser.close()
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'scope': 'Real Chrome page with a simulated MCP Apps host; ChatGPT browser verification is separate',
              'panel_version': re.search(r"const VERSION = '([^']+)'", PANEL)[1].replace(' ', '-'), 'screenshots': sorted(str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p) for p in SHOTS.glob('*.png')),
              'checks': checks, 'passed': sum(c['passed'] for c in checks), 'total': len(checks)}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('passed', 'total', 'screenshots')}, ensure_ascii=False, indent=2))
    assert report['passed'] == report['total'], [c['name'] for c in checks if not c['passed']]


if __name__ == '__main__':
    main()
