"""Real MCP regressions for job timing, terminal task clocks, and historical acceptance."""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from mcp import Client, StdioServerParameters


async def verify():
    source = Path(__file__).resolve().parents[1]
    checks = []

    def check(name, passed, evidence):
        checks.append({'name': name, 'passed': bool(passed), 'evidence': evidence})

    async def call(client, name, **arguments):
        result = await client.call_tool(name, arguments)
        assert not result.is_error, (name, result.content)
        return result.structured_content

    with tempfile.TemporaryDirectory(prefix='localpilot-regressions-') as directory:
        root = Path(directory).resolve()
        work = root / 'work'; work.mkdir()
        (work / 'result.txt').write_text('ready')
        config = root / 'config.json'
        config.write_text(json.dumps({'workspaces': {'project': str(work)}, 'state_dir': str(root / 'state')}))
        params = StdioServerParameters(command=sys.executable, args=['-I', str(source / 'agent/server.py')],
                                       env={'LOCALPILOT_CONFIG': str(config)})
        plan = [{'id': 'verify', 'step': 'Run verification', 'status': 'in_progress'}]
        async with Client(params) as client:
            task = await call(client, 'create_task', workspace='project', objective='Verify command timing.',
                              plan=plan, checks=[{'kind': 'job_succeeded', 'action_id': 'verify'}])
            task_id = task['task_id']
            launched = await call(client, 'run_task_step', task_id=task_id, step_id='verify', operation='run_shell',
                                  arguments={'command': 'sleep 1.2; printf finished', 'timeout_seconds': 10}, action_id='verify')
            running = (await call(client, 'get_task_activity', task_id=task_id, action_id='verify'))['records'][0]
            check('running_command_has_no_fake_final_duration', running['outcome'] == 'running' and running['duration_seconds'] is None,
                  {'outcome': running['outcome'], 'duration_seconds': running['duration_seconds']})
            job = await call(client, 'job_status', job_id=launched['action']['result']['job_id'], wait_seconds=5)
            record = (await call(client, 'get_task_activity', task_id=task_id, action_id='verify'))['records'][0]
            actual = round(job['finished_at'] - job['started_at'], 3)
            check('shell_duration_matches_job_not_launch_latency', job['status'] == 'completed' and actual >= 1.2 and
                  abs(record['duration_seconds'] - actual) <= .001,
                  {'actual_job_seconds': actual, 'duration_seconds': record['duration_seconds'],
                   'launch_seconds': round(launched['action']['finished_at'] - launched['action']['started_at'], 3)})
            await call(client, 'update_plan', task_id=task_id, expected_revision=launched['task']['revision'],
                       plan=[dict(plan[0], status='completed')])
            completed = (await call(client, 'finish_task', task_id=task_id))['task']

            file_task = await call(client, 'create_task', workspace='project', objective='Preserve historical acceptance.',
                                   plan=[dict(plan[0], status='completed')],
                                   checks=[{'kind': 'file_equals', 'path': 'result.txt', 'value': 'ready'}])
            file_done = (await call(client, 'finish_task', task_id=file_task['task_id']))['task']
            cancelled = await call(client, 'create_task', workspace='project', objective='Cancel without resetting its clock.',
                                   plan=plan, checks=[{'kind': 'file_equals', 'path': 'result.txt', 'value': 'ready'}])
            cancelled = await call(client, 'set_task_state', task_id=cancelled['task_id'], status='cancelled', reason='Regression test')
            await asyncio.sleep(1.2)
            for before, name in [(completed, 'completed'), (cancelled, 'cancelled')]:
                after = await call(client, 'get_task', task_id=before['task_id'])
                check(f'{name}_elapsed_time_is_frozen', after['elapsed_seconds'] == before['elapsed_seconds'] and
                      after.get('finished_at') == before.get('finished_at') and after.get('finished_at') is not None,
                      {'first': before['elapsed_seconds'], 'later': after['elapsed_seconds'], 'finished_at': after.get('finished_at')})
            (work / 'result.txt').write_text('changed after completion')
            changed = await call(client, 'get_task', task_id=file_done['task_id'])
            check('live_failed_checks_preserve_historical_acceptance', changed['status'] == 'complete' and
                  changed['checks_status']['passed'] == 0 and changed['final_review'] == file_done['final_review'],
                  {'status': changed['status'], 'live_passed': changed['checks_status']['passed'],
                   'historical_passed': changed['final_review']['checks'][0]['passed']})

        # Existing installations have terminal snapshots without finished_at. No migration may rewrite their evidence.
        database = next((root / 'state').glob('*.sqlite3'))
        with sqlite3.connect(database) as db:
            old = json.loads(db.execute('SELECT snapshot FROM tasks WHERE id=?', (task_id,)).fetchone()[0])
            old.pop('finished_at', None)
            db.execute('UPDATE tasks SET snapshot=? WHERE id=?', (json.dumps(old), task_id))
        async with Client(params) as client:
            restored = await call(client, 'get_task', task_id=task_id)
            check('legacy_completed_clock_uses_persisted_end_time', restored['elapsed_seconds'] == round(old['updated_at'] - old['created_at']),
                  {'elapsed': restored['elapsed_seconds'], 'expected': round(old['updated_at'] - old['created_at'])})
            restored_cancel = await call(client, 'get_task', task_id=cancelled['task_id'])
            check('terminal_clock_survives_restart', restored_cancel['elapsed_seconds'] == cancelled['elapsed_seconds'] and
                  restored_cancel.get('finished_at') == cancelled.get('finished_at'),
                  {'before': cancelled['elapsed_seconds'], 'after': restored_cancel['elapsed_seconds']})
    return {'checked_at': datetime.now(timezone.utc).isoformat(), 'scope': 'Real MCP, temporary workspace and database; no model calls',
            'checks': checks, 'passed': sum(c['passed'] for c in checks), 'total': len(checks)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'verification/regressions-r10.json')
    args = parser.parse_args()
    report = asyncio.run(verify())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report['passed'] == report['total'] else 1)
