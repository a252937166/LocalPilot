"""Exercise the model-free Chat harness through real MCP; only temporary files."""
import asyncio
import argparse
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone
from mcp import Client, StdioServerParameters


async def main(output):
    source = Path(__file__).resolve().parents[1]
    checks = []
    def record(name, ok):
        checks.append({'name': name, 'passed': bool(ok)})
    async def call(client, name, **args):
        result = await client.call_tool(name, args)
        assert not result.is_error, (name, result.content)
        return result.structured_content
    async def rejected(client, name, **args):
        return (await client.call_tool(name, args)).is_error
    with tempfile.TemporaryDirectory(prefix='localpilot-harness-') as folder:
        root = Path(folder).resolve(); work = root/'workspace'; work.mkdir()
        config = root/'config.json'
        config.write_text(json.dumps({'workspaces': {'project': str(work)}, 'state_dir': str(root/'state')}))
        params = StdioServerParameters(command=sys.executable, args=['-I', str(source/'agent/server.py')], env={'LOCALPILOT_CONFIG': str(config)})
        async with Client(params) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            record('harness_tools_and_private_continuation', all(n in tools for n in ['create_task','get_task','update_plan','run_task_step','review_task','finish_task','set_task_state','show_task_panel']) and tools['claim_task_continuation'].meta['ui']['visibility'] == ['app'])
            record('create_task_renders_panel_before_execution', tools['create_task'].meta['ui']['resourceUri'] == tools['show_task_panel'].meta['ui']['resourceUri'])
            resource = await client.read_resource(tools['show_task_panel'].meta['ui']['resourceUri'])
            record('panel_resource_and_csp', resource.contents[0].mime_type == 'text/html;profile=mcp-app' and 'ui/message' in resource.contents[0].text and resource.contents[0].meta['ui']['csp']['connectDomains'] == [] and 'v0.6.9-r30' in tools['show_task_panel'].meta['ui']['resourceUri'])
            record('run_task_step_has_status_strings_but_no_card_by_default', tools['run_task_step'].meta['openai/toolInvocation/invoking'] and 'openai/outputTemplate' not in tools['run_task_step'].meta)
            receipt = await client.read_resource('ui://localpilot/task-step-receipt-v0.6.9-r30.html')
            record('receipt_card_resource_readable', 'ui/notifications/tool-result' in receipt.contents[0].text and receipt.contents[0].meta['ui']['csp']['connectDomains'] == [])
            oldest = await client.read_resource('ui://localpilot/task-panel-v0.3.0-r2.html')
            record('old_r2_template_still_readable', oldest.contents[0].text == resource.contents[0].text)
            legacy_panel = await client.read_resource('ui://localpilot/task-panel-v0.6.9-r28.html')
            legacy_card = await client.read_resource('ui://localpilot/task-step-receipt-v0.6.9-r28.html')
            record('cached_panel_uri_serves_current_code_and_csp', legacy_panel.contents[0].text == resource.contents[0].text and legacy_panel.contents[0].meta['ui']['csp'] == resource.contents[0].meta['ui']['csp'])
            record('cached_receipt_uri_serves_current_code_and_csp', legacy_card.contents[0].text == receipt.contents[0].text and legacy_card.contents[0].meta['ui']['csp'] == receipt.contents[0].meta['ui']['csp'])
            record('job_status_accepts_wait_seconds', 'wait_seconds' in tools['job_status'].input_schema['properties'] and 'action_id' in tools['get_task_activity'].input_schema['properties'])
            plan = [{'id':'edit','step':'Create file','status':'in_progress'}, {'id':'verify','step':'Verify result','status':'pending'}]
            task = await call(client, 'create_task', workspace='project', objective='Create result.txt containing ready and verify it.', plan=plan,
                              checks=[{'kind':'file_equals','path':'result.txt','value':'ready\n'}, {'kind':'job_succeeded','action_id':'verify'}])
            task_id = task['task_id']
            record('premature_finish_refused', not (await call(client,'finish_task',task_id=task_id))['completed'])
            record('inactive_step_refused', await rejected(client,'run_task_step',task_id=task_id,step_id='verify',operation='read_file',arguments={'path':'result.txt'},action_id='bad-step'))
            record('cross_workspace_override_refused', await rejected(client,'run_task_step',task_id=task_id,step_id='edit',operation='write_file',arguments={'workspace':'elsewhere','path':'result.txt','content':'bad'},action_id='bad-root'))
            record('two_active_steps_refused', await rejected(client,'update_plan',task_id=task_id,expected_revision=task['revision'],plan=[dict(p,status='in_progress') for p in plan]))
            result = await call(client,'run_task_step',task_id=task_id,step_id='edit',operation='write_file',arguments={'path':'result.txt','content':'ready\n'},action_id='write')
            task = result['task']; sha = result['action']['result']['sha256']
            same = await call(client,'run_task_step',task_id=task_id,step_id='edit',operation='write_file',arguments={'path':'result.txt','content':'ready\n'},action_id='write')
            record('action_replay_does_not_execute_twice', same['replayed'] and same['task']['action_count'] == 1 and (work/'result.txt').read_text() == 'ready\n')
            record('action_id_conflict_refused', await rejected(client,'run_task_step',task_id=task_id,step_id='edit',operation='write_file',arguments={'path':'result.txt','content':'other'},action_id='write'))
            record('stale_plan_revision_refused', await rejected(client,'update_plan',task_id=task_id,plan=plan,expected_revision=1))
            plan = [dict(plan[0],status='completed'), dict(plan[1],status='in_progress')]
            task = await call(client,'update_plan',task_id=task_id,plan=plan,expected_revision=task['revision'])
            failed = await call(client,'run_task_step',task_id=task_id,step_id='verify',operation='run_shell',arguments={'command':'exit 7'},action_id='verify')
            record('failed_command_is_not_completion', failed['action']['result']['exit_code'] == 7 and not (await call(client,'finish_task',task_id=task_id))['completed'])
            activity = await call(client,'get_task_activity',task_id=task_id)
            record('activity_exposes_real_exit_and_command_without_file_body', activity['records'][0]['result']['exit_code'] == 7 and activity['records'][0]['inputs']['command'] == 'exit 7' and all('content' not in r['result'] for r in activity['records']))
            record('activity_records_outcome_and_duration', activity['records'][0]['outcome'] == 'failed' and isinstance(activity['records'][0]['duration_seconds'], float))
            verified = await call(client,'run_task_step',task_id=task_id,step_id='verify',operation='run_shell',arguments={'command':'test -s result.txt'},action_id='verify:2')
            record('corrected_verification_reuses_criterion', (await call(client,'review_task',task_id=task_id))['checks'][1]['passed'])
            status_receipt = await call(client,'run_task_step',task_id=task_id,step_id='verify',operation='job_status',arguments={'job_id':verified['action']['result']['job_id'],'wait_seconds':5},action_id='verify:2:status')
            record('status_receipt_named_after_verification_does_not_shadow_it', status_receipt['action']['result']['status'] == 'completed' and (await call(client,'review_task',task_id=task_id))['checks'][1]['passed'])
            slow = await call(client,'run_task_step',task_id=task_id,step_id='verify',operation='run_shell',arguments={'command':'sleep 1.5; echo slept','timeout_seconds':10},action_id='slow')
            waited = await call(client,'run_task_step',task_id=task_id,step_id='verify',operation='job_status',arguments={'job_id':slow['action']['result']['job_id'],'wait_seconds':10},action_id='slow-wait')
            record('job_status_long_poll_returns_terminal_state', slow['action']['result']['status'] == 'running' and waited['action']['result']['status'] == 'completed' and waited['action']['result']['stdout'] == 'slept\n')
            record('job_status_wait_bounds_enforced', await rejected(client,'job_status',job_id=slow['action']['result']['job_id'],wait_seconds=21))
            verified = await call(client,'run_task_step',task_id=task_id,step_id='verify',operation='run_shell',arguments={'command':'test -s result.txt'},action_id='verify:3')
            live = await call(client,'get_task',task_id=task_id)
            record('step_results_are_compact', 'objective' not in verified['task'] and 'checks' not in verified['task'] and verified['task']['checks_status']['checks'][1]['passed'] and verified['task']['revision'] == live['revision'])
            record('get_task_reports_live_checks_step_stats_and_clock', live['checks_status']['total'] == 2 and live['checks_status']['checks'][1]['passed'] and live['step_stats']['verify']['actions'] == 6 and live['step_stats']['verify']['failed'] == 1 and isinstance(live['idle_seconds'], (int, float)) and live['remaining_seconds'] > 0 and live['features'] == {'step_cards': False} and live['device_label'] == 'My Mac')
            record('plan_items_stay_round_trippable', all(set(p) == {'id','step','status'} for p in live['plan']))
            with sqlite3.connect(str(root/'state/localpilot.sqlite3')) as db:
                events_before = db.execute('select count(*) from events').fetchone()[0]
            await call(client,'get_task',task_id=task_id); await call(client,'get_task',task_id=task_id)
            with sqlite3.connect(str(root/'state/localpilot.sqlite3')) as db:
                events_after = db.execute('select count(*) from events').fetchone()[0]
            record('status_polling_writes_no_audit_receipts', events_after == events_before)
            edited = await call(client,'run_task_step',task_id=task_id,step_id='verify',operation='replace_text',arguments={'path':'result.txt','old_text':'ready','new_text':'ready','expected_sha256':sha},action_id='edit-preview')
            detail = await call(client,'get_task_activity',task_id=task_id,action_id='edit-preview')
            listing = await call(client,'get_task_activity',task_id=task_id,limit=50)
            record('edit_receipts_carry_bounded_previews_not_file_bodies', detail['records'][0]['inputs']['old_text'] == 'ready' and detail['records'][0]['inputs']['new_text_chars'] == 5 and detail['records'][0]['result']['previous_sha256'] == sha and 'content' not in detail['records'][0]['result'] and listing['total'] == len(listing['records']))
            record('unknown_activity_action_id_rejected', await rejected(client,'get_task_activity',task_id=task_id,action_id='nope'))
            verified = await call(client,'run_task_step',task_id=task_id,step_id='verify',operation='run_shell',arguments={'command':'test -s result.txt'},action_id='verify:4')
            after = await call(client,'run_task_step',task_id=task_id,step_id='verify',operation='write_file',arguments={'path':'result.txt','content':'ready\n','expected_sha256':sha},action_id='touch-after-verification')
            record('mutation_invalidates_old_verification', not (await call(client,'review_task',task_id=task_id))['checks'][1]['passed'] and not after['task']['checks_status']['checks'][1]['passed'])
            verified = await call(client,'run_task_step',task_id=task_id,step_id='verify',operation='run_shell',arguments={'command':'test -s result.txt'},action_id='verify:5')
            task = verified['task']
            plan = [dict(p,status='completed') for p in plan]
            task = await call(client,'update_plan',task_id=task_id,plan=plan,expected_revision=task['revision'])
            completed = await call(client,'finish_task',task_id=task_id)
            record('verified_task_completed', completed['completed'] and completed['task']['status'] == 'complete')
            record('completed_task_cannot_continue', await rejected(client,'claim_task_continuation',task_id=task_id,expected_revision=completed['task']['revision']))
            record('completed_task_cannot_execute', await rejected(client,'run_task_step',task_id=task_id,step_id='verify',operation='run_shell',arguments={'command':'echo bad'},action_id='after-complete'))
            task2 = await call(client,'create_task',workspace='project',objective='Wait for separately produced file.',plan=[{'id':'one','step':'Inspect file','status':'in_progress'}],checks=[{'kind':'file_equals','path':'later.txt','value':'done'}],max_continuations=1)
            first = await call(client,'claim_task_continuation',task_id=task2['task_id'],expected_revision=task2['revision'])
            duplicate = await call(client,'claim_task_continuation',task_id=task2['task_id'],expected_revision=task2['revision'])
            record('continuation_is_claimed_once', first['send'] and not duplicate['send'] and 'Work、Codex' in first['prompt'])
            paused = await call(client,'set_task_state',task_id=task2['task_id'],status='paused',reason='test pause')
            record('paused_task_blocks_execution', await rejected(client,'run_task_step',task_id=task2['task_id'],step_id='one',operation='read_file',arguments={'path':'result.txt'},action_id='paused-read'))
            resumed = await call(client,'set_task_state',task_id=task2['task_id'],status='active',reason='test resume')
            record('continuation_budget_survives_resume', not (await call(client,'claim_task_continuation',task_id=task2['task_id'],expected_revision=resumed['revision']))['send'])
            record('foreign_job_refused', await rejected(client,'run_task_step',task_id=task2['task_id'],step_id='one',operation='job_status',arguments={'job_id':verified['action']['result']['job_id']},action_id='foreign-job'))
            await call(client,'set_task_state',task_id=task2['task_id'],status='cancelled',reason='test complete')
            record('cancelled_task_cannot_resume', await rejected(client,'set_task_state',task_id=task2['task_id'],status='active',reason='invalid'))
            waiting = await call(client,'create_task',workspace='project',objective='Pause a test process.',plan=[{'id':'one','step':'Wait','status':'in_progress'}],checks=[{'kind':'job_succeeded','action_id':'wait'}])
            live = await call(client,'run_task_step',task_id=waiting['task_id'],step_id='one',operation='run_shell',arguments={'command':'sleep 15'},action_id='wait')
            await call(client,'set_task_state',task_id=waiting['task_id'],status='paused',reason='Pause process test')
            stopped = await call(client,'job_status',job_id=live['action']['result']['job_id'])
            record('pause_stops_owned_process', stopped['status'] == 'cancelled')
            limited = await call(client,'create_task',workspace='project',objective='Read the existing test file once.',plan=[{'id':'one','step':'Read','status':'in_progress'}],checks=[{'kind':'file_equals','path':'result.txt','value':'ready\n'}],max_actions=1)
            one = await call(client,'run_task_step',task_id=limited['task_id'],step_id='one',operation='read_file',arguments={'path':'result.txt'},action_id='one')
            record('action_limit_enforced', await rejected(client,'run_task_step',task_id=limited['task_id'],step_id='one',operation='read_file',arguments={'path':'result.txt'},action_id='two'))
            await call(client,'update_plan',task_id=limited['task_id'],plan=[{'id':'one','step':'Read','status':'completed'}],expected_revision=one['task']['revision'])
            record('budget_does_not_prevent_truthful_completion', (await call(client,'finish_task',task_id=limited['task_id']))['completed'])
        async with Client(params) as client:
            saved = await call(client,'get_task',task_id=task_id)
            record('goal_plan_and_completion_survive_restart', saved['status'] == 'complete' and saved['plan'] == plan and saved['objective'] == 'Create result.txt containing ready and verify it.')
        config.write_text(json.dumps({'workspaces': {'project': str(work)}, 'state_dir': str(root/'state'), 'step_cards': True}))
        async with Client(params) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            record('step_cards_config_attaches_receipt_card_to_run_task_step', tools['run_task_step'].meta.get('openai/outputTemplate') == 'ui://localpilot/task-step-receipt-v0.6.9-r30.html' and (await call(client,'get_task',task_id=task_id))['features'] == {'step_cards': True})
    report = {'checked_at':datetime.now(timezone.utc).isoformat(), 'scope':'Local MCP harness; no model API calls, not browser verification', 'checks': checks,
              'passed':sum(c['passed'] for c in checks),'total':len(checks)}
    destination = output or source/'verification/harness-latest.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    assert report['passed'] == report['total']


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    asyncio.run(main(parser.parse_args().output))
