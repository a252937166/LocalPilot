"""apply_patch, the local MCP bridge and skill readiness over real MCP stdio; temporary files only."""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

from mcp import Client, StdioServerParameters

FIXTURE_SERVER = '''
from mcp.server import MCPServer
mcp = MCPServer('Fixture')

@mcp.tool()
def echo(text: str) -> dict[str, str]:
    return {'echo': text}

@mcp.tool()
def big(n: int) -> str:
    return 'x' * n

@mcp.tool()
def boom() -> str:
    raise ValueError('boom')

if __name__ == '__main__':
    mcp.run()
'''


async def verify(output):
    source = Path(os.environ.get('LOCALPILOT_VERIFY_AGENT_ROOT', Path(__file__).resolve().parents[1])).absolute()
    checks = []
    def check(name, value):
        checks.append({'name': name, 'passed': bool(value)})
        assert value, name
    async def call(client, name, **args):
        result = await client.call_tool(name, args)
        assert not result.is_error, (name, result.content)
        return result.structured_content
    async def rejected(client, name, **args):
        return (await client.call_tool(name, args)).is_error
    with tempfile.TemporaryDirectory(prefix='localpilot-upgrades-') as directory:
        base = Path(directory).resolve(); work = base / 'project'; work.mkdir()
        (work / 'calc.py').write_text('def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b * 1\n')
        (work / 'old.txt').write_text('remove me\n')
        (work / 'crlf.txt').write_text('one\r\ntwo\r\nthree\r\n')
        (work / 'move.txt').write_text('keep\n')
        fixture = base / 'fixture_server.py'; fixture.write_text(FIXTURE_SERVER)
        toml = base / 'codex-config.toml'
        toml.write_text(f'[mcp_servers.fixture]\ncommand = "{sys.executable}"\nargs = ["{fixture}"]\n\n[mcp_servers.denied]\ncommand = "{sys.executable}"\nargs = ["{fixture}"]\n\n[mcp_servers.off]\ncommand = "{sys.executable}"\nargs = ["{fixture}"]\nenabled = false\n')
        (work / '.agents/skills/needs-cli').mkdir(parents=True)
        (work / '.agents/skills/needs-cli/SKILL.md').write_text('---\nname: needs-cli\ndescription: 用 `python3` 和 `no-such-cli-xyz` 处理账单。触发词：账单。\n---\nbody\n')
        state, config = base / 'state', base / 'config/config.json'
        config.parent.mkdir()
        config.write_text(json.dumps({'workspaces': {'project': str(work)}, 'state_dir': str(state), 'local_skill_roots': [],
                                      'mcp_bridge': {'enabled': True, 'config_files': [str(toml)], 'deny': ['denied'], 'max_output_chars': 5000}}))
        params = StdioServerParameters(command=sys.executable, args=['-I', str(source / 'agent/server.py')], env={'LOCALPILOT_CONFIG': str(config)})
        async with Client(params) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            check('new_tools_registered_with_annotations', tools['apply_patch'].annotations.read_only_hint is False and tools['list_mcp_servers'].annotations.read_only_hint and tools['call_mcp_tool'].annotations.open_world_hint)
            status = await call(client, 'device_status')
            check('device_status_reports_bridge_and_patch', status['mcp_bridge']['enabled'] and 'fixture' in status['mcp_bridge']['servers'] and 'apply_patch' in status['available_file_tools'])
            calc_sha = hashlib.sha256((work / 'calc.py').read_bytes()).hexdigest()
            patch = ('*** Begin Patch\n*** Update File: calc.py\n@@ def add(a, b):\n-    return a - b\n+    return a + b\n@@\n def mul(a, b):\n-    return a * b * 1\n+    return a * b\n'
                     '*** Add File: notes/new.txt\n+hello\n+world\n*** Delete File: old.txt\n*** Update File: move.txt\n*** Move to: moved/renamed.txt\n@@\n-keep\n+kept\n*** End Patch\n')
            applied = await call(client, 'apply_patch', workspace='project', patch=patch, expected_sha256={'calc.py': calc_sha})
            actions = {Path(f['path']).name: f['action'] for f in applied['files']}
            check('codex_patch_updates_adds_deletes_moves', actions == {'calc.py': 'updated', 'new.txt': 'created', 'old.txt': 'deleted', 'renamed.txt': 'moved'}
                  and (work / 'calc.py').read_text() == 'def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n'
                  and (work / 'notes/new.txt').read_text() == 'hello\nworld\n' and not (work / 'old.txt').exists() and not (work / 'move.txt').exists()
                  and (work / 'moved/renamed.txt').read_text() == 'kept\n' and applied['lines_added'] == 5 and applied['receipt_id'])
            unified = '--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return a + b + 0\n'
            after = await call(client, 'apply_patch', workspace='project', patch=unified)
            check('unified_diff_accepted', after['files'][0]['action'] == 'updated' and 'return a + b + 0' in (work / 'calc.py').read_text())
            before = (work / 'calc.py').read_text()
            check('mismatched_context_rejected_without_writing', await rejected(client, 'apply_patch', workspace='project', patch='*** Begin Patch\n*** Update File: calc.py\n@@\n-nothing like this\n+x\n*** End Patch\n') and (work / 'calc.py').read_text() == before)
            check('stale_sha_rejected', await rejected(client, 'apply_patch', workspace='project', patch=unified, expected_sha256=calc_sha))
            check('all_or_nothing_across_files', await rejected(client, 'apply_patch', workspace='project', patch='*** Begin Patch\n*** Add File: brand.txt\n+x\n*** Update File: calc.py\n@@\n-missing\n+y\n*** End Patch\n') and not (work / 'brand.txt').exists())
            crlf = await call(client, 'apply_patch', workspace='project', patch='*** Begin Patch\n*** Update File: crlf.txt\n@@\n two\n-three\n+3\n*** End of File\n*** End Patch\n')
            check('crlf_preserved', (work / 'crlf.txt').read_bytes() == b'one\r\ntwo\r\n3\r\n' and crlf['files'][0]['hunks_applied'] == 1)
            check('add_existing_file_rejected', await rejected(client, 'apply_patch', workspace='project', patch='*** Begin Patch\n*** Add File: calc.py\n+dup\n*** End Patch\n'))
            # MCP bridge
            servers = await call(client, 'list_mcp_servers')
            names = {s['name']: s for s in servers['servers']}
            check('bridge_lists_codex_servers_without_env_values', names['fixture']['allowed'] and names['fixture']['connected'] is False and not names['denied']['allowed'] and names['off']['enabled'] is False and 'env_keys' in names['fixture'])
            listed = await call(client, 'list_mcp_tools', server='fixture')
            check('bridge_connects_and_lists_tools', {t['name'] for t in listed['tools']} == {'echo', 'big', 'boom'} and 'input_schema' in listed['tools'][0])
            echoed = await call(client, 'call_mcp_tool', server='fixture', tool='echo', arguments={'text': 'hi'})
            check('bridge_call_returns_structured_content', echoed['structured_content'] == {'echo': 'hi'} and echoed['is_error'] is False and echoed['receipt_id'])
            large = await call(client, 'call_mcp_tool', server='fixture', tool='big', arguments={'n': 20000})
            check('bridge_output_bounded', large['truncated'] and len(large['text']) <= 5000)
            failed_result = await client.call_tool('call_mcp_tool', {'server':'fixture', 'tool':'boom'})
            failed = failed_result.structured_content
            check('remote_tool_error_preserves_protocol_flag_and_receipt', failed_result.is_error and failed['is_error'] is True and 'boom' in failed['text'] and failed['receipt_id'])
            check('unknown_denied_and_disabled_servers_rejected', await rejected(client, 'list_mcp_tools', server='nope') and await rejected(client, 'call_mcp_tool', server='denied', tool='echo') and await rejected(client, 'list_mcp_tools', server='off'))
            check('reconnect_reuses_session', (await call(client, 'list_mcp_servers'))['servers'][1]['connected'] is True)
            # readiness
            found = await call(client, 'find_skills', workspace='project', query='处理账单')
            readiness = found['matches'][0]['readiness']
            check('skill_readiness_checks_named_commands', found['matches'][0]['name'] == 'needs-cli' and readiness['ready'] is False and {c['name']: c['found'] for c in readiness['commands']} == {'python3': True, 'no-such-cli-xyz': False})
            loaded = await call(client, 'read_local_skill', workspace='project', skill_path=found['matches'][0]['skill_path'])
            check('read_local_skill_reports_readiness', loaded['readiness']['ready'] is False)
            # harness integration
            task = await call(client, 'create_task', workspace='project', objective='Patch and query through the bridge.',
                              plan=[{'id': 'work', 'step': 'Patch and call', 'status': 'in_progress'}],
                              checks=[{'kind': 'file_contains', 'path': 'calc.py', 'value': 'return a * b'}])
            patched = await call(client, 'run_task_step', task_id=task['task_id'], step_id='work', operation='apply_patch',
                                 arguments={'patch': '*** Begin Patch\n*** Update File: calc.py\n@@\n-    return a + b + 0\n+    return a + b\n*** End Patch\n'}, action_id='patch')
            observed = await call(client, 'inspect_task_step', task_id=task['task_id'], step_id='work', operation='list_mcp_tools', arguments={'server': 'fixture'}, action_id='mcp-list')
            called = await call(client, 'run_task_step', task_id=task['task_id'], step_id='work', operation='call_mcp_tool', arguments={'server': 'fixture', 'tool': 'echo', 'arguments': {'text': 'task'}}, action_id='mcp-call')
            check('task_patch_is_mutation_with_preview', patched['task']['action_count'] == 1 and patched['action']['inputs']['patch_files'] == ['calc.py (update)'] and 'patch_preview' in patched['action']['inputs'])
            check('task_bridge_observation_and_mutation', observed['task']['action_count'] == 1 and observed['task']['observation_count'] == 1 and called['task']['action_count'] == 2 and called['action']['result']['structured_content'] == {'echo': 'task'} and called['action']['inputs']['server'] == 'fixture')
            activity = await call(client, 'get_task_activity', task_id=task['task_id'])
            by_id = {r['action_id']: r for r in activity['records']}
            check('activity_shows_patch_and_mcp_facts', by_id['patch']['result']['files_count'] == 1 and 'summary' in by_id['patch']['result'] and by_id['mcp-call']['result']['tool'] == 'echo')
            with sqlite3.connect(state / 'localpilot.sqlite3') as db:
                events = [row[0] for row in db.execute("SELECT tool FROM events WHERE tool IN ('apply_patch','call_mcp_tool')")]
            check('receipts_recorded_for_patch_and_bridge', events.count('apply_patch') >= 5 and events.count('call_mcp_tool') >= 4)
        disabled = json.loads(config.read_text()); disabled['mcp_bridge']['enabled'] = False; config.write_text(json.dumps(disabled))
        async with Client(params) as client:
            check('bridge_disabled_by_config', await rejected(client, 'list_mcp_tools', server='fixture') and (await call(client, 'device_status'))['mcp_bridge']['enabled'] is False)
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'source': str(source), 'scope': 'Real local MCP; fixture MCP server; no model/API or browser claims',
              'checks': checks, 'passed': sum(c['passed'] for c in checks), 'total': len(checks)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'total': report['total'], 'report': str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'verification/v0.6.28-upgrades.json')
    asyncio.run(verify(parser.parse_args().output))
