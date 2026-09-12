"""Render the unchanged task panel with public demo data; no local-control operations."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import argparse
import json
import re
from verify_panel import fixtures, HOST, PANEL


def page():
    task, activity = deepcopy(fixtures('complete'))
    task['task_id'] = activity['task_id'] = 'demo-task-calculator'
    task['device_label'] = 'Demo Mac'
    task['objective'] = '修复 calculator.py 的加法错误，保留原测试，实际运行验证并完成验收。'
    task['max_actions'] = 40
    task['max_continuations'] = 3
    task['continuations'] = 0
    task['action_count'] = 2
    task['observation_count'] = 1
    task['auto_continue'] = False
    labels = {'repair': '修复 calculator.py', 'verify-fixed': '运行测试并检查退出码', 'audit-finish': '核对结果并完成验收'}
    task['plan'] = [{'id': 'read', 'step': '读取项目规则、技能与源文件', 'status': 'completed'}] + [
        {'id': k, 'step': v, 'status': 'completed'} for k, v in labels.items()]
    activity['records'] = [r for r in activity['records'] if r['action_id'] in ('read-calculator-before-repair', 'repair-add', 'verify:2')]
    activity['records'][-1]['step_id'] = 'read'
    for record in activity['records']:
        if record['operation'] == 'run_shell':
            record['result']['stdout'] = '3 checks passed\nLOCALPILOT_DEMO_PASS\n'
    task['step_stats'] = {}
    for plan in task['plan']:
        records = [r for r in activity['records'] if r['step_id'] == plan['id']]
        if records:
            last = max(records, key=lambda r: r['finished_at'])
            task['step_stats'][plan['id']] = {'actions': len(records), 'failed': 0, 'last': {
                'action_id': last['action_id'], 'operation': last['operation'], 'outcome': last['outcome'],
                'exit_code': last['result'].get('exit_code'), 'finished_at': last['finished_at']}}
    activity['total'] = len(activity['records'])
    payload = json.dumps([task, activity], ensure_ascii=False).replace('/Users/me/project/selftest/chat-harness-1788935922', '/demo/calculator').replace('selftest/chat-harness-1788935922/', '')
    payload = re.sub(r'(?<![a-zA-Z0-9])[a-f0-9]{32}(?![a-zA-Z0-9])', 'demo-receipt', payload)
    title = '''<header style="padding:24px 40px 16px;font:14px -apple-system,BlinkMacSystemFont,sans-serif;color:#777">
    <strong style="color:#171717;letter-spacing:.05em">LOCALPILOT</strong> / WEB CHAT → LOCAL AI CODING
    <span style="float:right">实际组件 · 公开演示数据</span></header>'''
    html = HOST.replace('__BG__', '#fff').replace('<head>', '<head><title>LocalPilot demo</title><style>body{max-width:1100px;margin:0 auto!important}header{padding-left:20px!important;padding-right:20px!important}</style>').replace('<iframe id="f"', title + '<iframe title="LocalPilot demo" id="f"')
    panel = json.dumps(PANEL, ensure_ascii=False).replace('</', '<\\/')
    boot = '<script>[window.__task,window.__activity]=' + payload + ';window.__mount(' + panel + ');</script>'
    return html.replace('</body>', boot + '</body>').encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    body = page()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ('/', '/index.html'):
                self.send_error(404); return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Public-data demo: http://127.0.0.1:{server.server_port}', flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == '__main__':
    main()
