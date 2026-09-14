"""Local file URL policy and navigation-race regressions; no Chrome or model calls."""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(os.environ.get('LOCALPILOT_VERIFY_AGENT_ROOT', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / 'agent'))
from browser_control import BrowserControl
from filesystem import Files
from mcp.server.mcpserver.exceptions import ToolError


def verify(output):
    checks = []
    def check(name, passed):
        checks.append({'name': name, 'passed': bool(passed)})
        assert passed, name
    def denied(call):
        try:
            call()
            return False
        except ToolError:
            return True

    with tempfile.TemporaryDirectory(prefix='localpilot-browser-paths-') as directory:
        base = Path(directory).resolve()
        work, protected = base / 'project', base / 'protected'
        work.mkdir(); protected.mkdir()
        allowed = work / '中文 #1.txt'; allowed.write_text('allowed fixture')
        outside = base / 'outside.txt'; outside.write_text('outside fixture')
        sensitive = protected / 'secret.txt'; sensitive.write_text('protected fixture')
        hidden = work / '.ssh'; hidden.mkdir()
        (hidden / 'alias').symlink_to(allowed)
        (work / 'alias').symlink_to(allowed)
        (work / 'escape').symlink_to(outside)
        (work / 'protected-alias').symlink_to(sensitive)
        hard = work / 'hard'; os.link(outside, hard)
        (work / '.env.local').write_text('environment fixture')
        (work / 'nested').mkdir()
        settings = {'state_dir': str(base / 'state'), 'permission_mode': 'workspace',
                    'protected_paths': [str(protected)], 'workspaces': {'project': str(work)},
                    'max_file_bytes': 10000,
                    'browser': {'enabled': True, 'cdp_port': 9222, 'cdp_url': None,
                                'profile_dir': str(base / 'profile'), 'headless': True,
                                'launch_if_missing': False, 'navigation_timeout_ms': 1000,
                                'snapshot_max_chars': 12000, 'screenshot_max_side': 1600}}
        control = BrowserControl(settings, None)
        files = Files(settings, SimpleNamespace(path=base / 'state' / 'test.sqlite'))
        check('encoded_workspace_file_allowed', not denied(lambda: control._authorize_url(allowed.as_uri())))
        check('localhost_authority_case_insensitive', not denied(lambda: control._authorize_url(allowed.as_uri().replace('file://', 'file://LOCALHOST'))))
        for name, path in [('outside', outside), ('symlink', work / 'alias'), ('symlink_escape', work / 'escape'),
                           ('hidden_symlink', hidden / 'alias'), ('hardlink', hard), ('environment', work / '.env.local')]:
            check(name + '_matches_file_tools', denied(lambda: files.peek('project', str(path)))
                  and denied(lambda: control._authorize_url(path.as_uri())))
        for name, url in [('relative_url', 'file:relative.txt'), ('empty_path', 'file://localhost'),
                          ('remote_file_host', 'file://other-host/tmp/file'), ('bad_encoding', work.as_uri() + '/%FF'),
                          ('null_byte', work.as_uri() + '/%00'), ('traversal', work.as_uri() + '/nested/../中文%20%231.txt')]:
            check(name + '_rejected', denied(lambda: control._authorize_url(url)))
        check('missing_allowed_file_left_to_browser', not denied(lambda: control._authorize_url((work / 'missing.txt').as_uri())))
        full = BrowserControl({**settings, 'permission_mode': 'full_machine'}, None)
        check('full_machine_outside_workspace_allowed', not denied(lambda: full._authorize_url(outside.as_uri())))
        check('full_machine_environment_allowed', not denied(lambda: full._authorize_url((work / '.env.local').as_uri())))
        check('full_machine_symlink_allowed', not denied(lambda: full._authorize_url((work / 'alias').as_uri())))
        check('full_machine_protected_path_denied', denied(lambda: full._authorize_url(sensitive.as_uri())))
        check('full_machine_protected_symlink_denied', denied(lambda: full._authorize_url((work / 'protected-alias').as_uri())))
        for name, mode in [('workspace', control), ('full_machine', full)]:
            page = SimpleNamespace(url='https://fixture.example/', frames=[SimpleNamespace(url=sensitive.as_uri())])
            check(name + '_protected_subframe_denied', denied(lambda: mode._authorize_page(page)))

        class RacePage:
            url = allowed.as_uri()
            frames = []
            calls = 0
            async def evaluate(self, *args):
                return {'width': 100, 'height': 100, 'dpr': 1, 'pageWidth': 100, 'pageHeight': 100}
            async def screenshot(self, **kwargs):
                self.url = sensitive.as_uri()
                return b'forbidden screenshot must never be decoded or saved'
        race = RacePage()
        async def get_page(*args): return race
        control._page = get_page
        check('screenshot_navigation_race_rejected', denied(lambda: control.screenshot()))
        check('rejected_screenshot_not_persisted', not (control.state / 'screenshots').exists())

        class SnapshotPage:
            url = allowed.as_uri()
            frames = []
            calls = 0
            async def evaluate(self, *args):
                self.calls += 1
                self.url = sensitive.as_uri()
                raise RuntimeError('Execution context was destroyed by navigation')
            async def wait_for_load_state(self, *args, **kwargs): pass
        retry = SnapshotPage()
        check('snapshot_retry_reauthorizes_page', denied(lambda: asyncio.run(control._snapshot(retry))) and retry.calls == 1)
        control.loop.call_soon_threadsafe(control.loop.stop)

    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'scope': __doc__,
              'checks': checks, 'passed': sum(c['passed'] for c in checks), 'total': len(checks)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'total': report['total'], 'report': str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'verification/browser-paths.json')
    verify(parser.parse_args().output)
