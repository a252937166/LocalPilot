"""Real MCP checks for full-machine mode and detached long jobs, using disposable files only."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from mcp import Client, StdioServerParameters


async def main(args):
    source = Path(__file__).resolve().parents[1]
    root = Path(tempfile.mkdtemp(prefix='localpilot-r12-runtime-')).resolve()
    work, outside, control = root/'project', root/'outside', root/'control'
    for p in (work, outside, control): p.mkdir()
    (outside/'.config').mkdir(); (outside/'.config/probe.txt').write_text('outside-ok\n')
    (work/'external-link').symlink_to(outside, target_is_directory=True)
    (work/'probe.py').write_text('import time\nprint("PROBE_START",flush=True)\ntime.sleep(8)\nprint("PROBE_PASS",flush=True)\n')
    config=control/'config.json'
    config.write_text(json.dumps({'workspaces':{'project':str(work)},'state_dir':str(control/'state'),
                                  'permission_mode':'full_machine','durable_jobs':True}))
    params=StdioServerParameters(command=sys.executable,args=['-I',str(source/'agent/server.py')],env={'LOCALPILOT_CONFIG':str(config)})
    checks=[]
    def record(name, passed, evidence=None):
        checks.append({'name':name,'passed':bool(passed),'evidence':evidence})
    async def call(client,name,**kwargs):
        r=await client.call_tool(name,kwargs)
        if r.is_error: raise AssertionError((name,r.content))
        return r.structured_content
    async with Client(params) as client:
        device=await call(client,'device_status')
        record('full_machine_and_multihour_limits',device['workspaces']['machine']=='/' and device['durable_jobs'] and device['shell_timeout']['maximum_seconds']==86400)
        read=await call(client,'read_file',workspace='project',path=str(outside/'.config/probe.txt'))
        record('outside_project_and_hidden_config_read',read['content']=='outside-ok\n')
        link=await call(client,'read_file',workspace='project',path='external-link/.config/probe.txt')
        record('symlink_resolves_under_full_machine_policy',link['sha256']==read['sha256'])
        write=await call(client,'replace_text',workspace='machine',path=str(outside/'.config/probe.txt'),old_text='outside-ok',new_text='outside-updated',expected_sha256=read['sha256'])
        record('outside_project_write',(outside/'.config/probe.txt').read_text()=='outside-updated\n')
        denied=await client.call_tool('read_file',{'workspace':'machine','path':str(config)})
        record('controller_config_stays_protected',denied.is_error)
        search=await call(client,'search_files',workspace='machine',path=str(outside),pattern='*probe.txt',query='updated')
        record('scoped_full_machine_search',len(search['results'])==1)
        if args.edges:
            missing=await client.call_tool('read_file',{'workspace':'machine','path':str(outside/'missing.txt')})
            record('missing_file_is_not_misreported_as_unauthorized',missing.is_error and '文件或目录不存在' in str(missing.content) and '拒绝访问' not in str(missing.content))
            locked=outside/'locked';locked.mkdir();(locked/'private.txt').write_text('disposable permission probe')
            locked.chmod(0)
            try:
                denied=await client.call_tool('read_file',{'workspace':'machine','path':str(locked/'private.txt')})
                record('os_permission_error_is_distinct',denied.is_error and 'macOS 账号或系统隐私设置拒绝访问' in str(denied.content))
                partial=await call(client,'search_files',workspace='machine',path=str(outside),pattern='*')
                record('search_reports_unreadable_subdirectories',partial['errors_count']==1 and str(locked) in str(partial['errors']))
            finally:
                locked.chmod(0o700)
        plan=[{'id':'read','step':'inspect outside file','status':'in_progress'},{'id':'verify','step':'verify actual worker','status':'pending'}]
        task=await call(client,'create_task',workspace='project',objective='Inspect the disposable external fixture and verify a detached worker across a real MCP reconnect.',plan=plan,
                        checks=[{'kind':'job_succeeded','action_id':'verify','value':'PROBE_PASS','min_duration_seconds':7}])
        task_id=task['task_id']
        record('four_hour_default_and_final_assessment',task['deadline']-task['created_at']==14400 and task['max_actions']==1000 and task['max_continuations']==80 and task['completion_assessment_required'])
        observed=await call(client,'inspect_task_step',task_id=task_id,step_id='read',operation='read_file',arguments={'path':str(outside/'.config/probe.txt')},action_id='read')
        record('read_only_steps_do_not_spend_action_budget',observed['task']['action_count']==0 and observed['task']['observation_count']==1)
        blocked=await client.call_tool('inspect_task_step',{'task_id':task_id,'step_id':'read','operation':'run_shell','arguments':{'command':'false'},'action_id':'no-shell'})
        record('readonly_tool_rejects_shell',blocked.is_error)
        plan[0]['status']='completed';plan[1]['status']='in_progress'
        job=await call(client,'run_task_step',task_id=task_id,step_id='verify',operation='run_shell',arguments={'command':'python3 probe.py','timeout_seconds':901},action_id='verify',
                       plan=plan,expected_revision=observed['task']['revision'],explanation='External file and symlink were verified; now run the detached validation.')
        job_id=job['action']['result']['job_id']
        record('atomic_plan_and_step_and_long_timeout',job['task']['plan']==plan and job['action']['result']['status']=='running' and job['action']['result']['timeout_seconds']==901)
        await call(client,'set_task_automation',task_id=task_id,enabled=True)
    # The MCP process has exited. The detached worker must remain alive and produce its own terminal receipt.
    async with Client(params) as client:
        restored=await call(client,'get_task',task_id=task_id)
        record('automation_and_running_job_survive_mcp_restart',restored['auto_continue'] and restored['checks_status']['running_jobs'])
        result=await call(client,'job_status',job_id=job_id,wait_seconds=20)
        record('detached_worker_completes_after_restart',result['status']=='completed' and result['exit_code']==0 and 'PROBE_PASS' in result['stdout'],result)
        replay=await call(client,'run_task_step',task_id=task_id,step_id='verify',operation='run_shell',arguments={'command':'python3 probe.py','timeout_seconds':901},action_id='verify')
        record('retry_returns_same_current_job',replay['replayed'] and replay['action']['result']['job_id']==job_id and replay['action']['result']['status']=='completed')
        plan[1]['status']='completed'
        await call(client,'update_plan',task_id=task_id,plan=plan,expected_revision=restored['revision'],explanation='Actual worker output and duration passed after the reconnect.')
        premature=await call(client,'finish_task',task_id=task_id)
        record('finish_requires_scope_assessment',not premature['completed'] and premature['review']['assessment_required'])
        assessment={'scope_summary':'Verified the originally requested external file and the actual worker output after reconnect, with unchanged task limits.','evidence_action_ids':['read','verify'],'remaining_work':['Deliberate test gap']}
        gaps=await call(client,'finish_task',task_id=task_id,assessment=assessment)
        record('declared_remaining_work_blocks_completion',not gaps['completed'])
        assessment['remaining_work']=[]
        done=await call(client,'finish_task',task_id=task_id,assessment=assessment)
        record('evidence_backed_assessment_completes',done['completed'] and not done['task']['auto_continue'])
        timeout=await call(client,'run_shell',workspace='project',command='sleep 30',timeout_seconds=1,request_id='timeout')
        timeout=await call(client,'job_status',job_id=timeout['job_id'],wait_seconds=10)
        record('worker_enforces_timeout',timeout['status']=='timed_out')
        stop=await call(client,'run_shell',workspace='project',command='sleep 30',timeout_seconds=60,request_id='cancel')
        stop=await call(client,'cancel_job',job_id=stop['job_id'])
        record('worker_cancels_promptly',stop['status']=='cancelled')
        if args.edges:
            shell_file=outside/'shell-proof.txt'
            shell=await call(client,'run_shell',workspace='machine',cwd=str(outside),command="printf shell-outside > shell-proof.txt && cat shell-proof.txt",timeout_seconds=15,request_id='full-machine-shell')
            shell=await call(client,'job_status',job_id=shell['job_id'],wait_seconds=15)
            record('full_machine_shell_can_write_outside_project',shell['status']=='completed' and shell_file.read_text()=='shell-outside')
            protected=await call(client,'run_shell',workspace='machine',command='cat '+shlex.quote(str(config)),timeout_seconds=15,request_id='protected-controller')
            protected=await call(client,'job_status',job_id=protected['job_id'],wait_seconds=15)
            record('shell_cannot_read_controller_configuration',protected['status']=='failed' and protected['exit_code']!=0 and 'Operation not permitted' in protected['stderr'])
            tail=await call(client,'run_shell',workspace='project',command="python3 -c 'print(\"x\"*100000);print(\"TAIL_OK\")'",timeout_seconds=15,request_id='bounded-tail')
            tail=await call(client,'job_status',job_id=tail['job_id'],wait_seconds=15)
            record('bounded_output_retains_final_verification_marker',tail['status']=='completed' and tail['output_truncated'] and tail['stdout_bytes']>100000 and len(tail['stdout'].encode())<=65536 and tail['stdout'].endswith('TAIL_OK\n'))
            maximum=await client.call_tool('run_shell',{'workspace':'project','command':'true','timeout_seconds':86401})
            record('timeout_above_twenty_four_hours_is_rejected',maximum.is_error)
            pause_task=await call(client,'create_task',workspace='project',objective='Verify pause stops this disposable detached job and preserves its original task state.',plan=[{'id':'run','step':'Run and pause','status':'in_progress'}],checks=[{'kind':'job_succeeded','action_id':'pause-job'}])
            launched=await call(client,'run_task_step',task_id=pause_task['task_id'],step_id='run',operation='run_shell',arguments={'command':'sleep 60','timeout_seconds':90},action_id='pause-job')
            awake_pid=launched['action']['result'].get('caffeinate_pid')
            assertions=subprocess.run(['/usr/bin/pmset','-g','assertions'],capture_output=True,text=True).stdout
            record('active_job_prevents_idle_system_sleep',awake_pid and f'pid {awake_pid}(caffeinate)' in assertions)
            await call(client,'set_task_automation',task_id=pause_task['task_id'],enabled=True)
            paused=await call(client,'set_task_state',task_id=pause_task['task_id'],status='paused',reason='Verify cancellation during a running detached task.')
            cancelled=await call(client,'job_status',job_id=launched['action']['result']['job_id'])
            record('task_pause_cancels_worker_and_disables_continuation',paused['status']=='paused' and not paused['auto_continue'] and cancelled['status']=='cancelled' and not paused['checks_status']['running_jobs'])
            awake_gone=subprocess.run(['/bin/ps','-p',str(awake_pid),'-o','command='],capture_output=True,text=True)
            record('sleep_assertion_released_after_cancellation',awake_gone.returncode!=0)
            restored=await call(client,'set_task_state',task_id=pause_task['task_id'],status='active',reason='Resume the saved disposable task for this regression check.')
            record('resume_preserves_limits_and_does_not_restart_command',restored['deadline']==pause_task['deadline'] and restored['action_count']==1 and not restored['auto_continue'] and not restored['checks_status']['running_jobs'])
        if args.soak_seconds:
            (work/'soak.py').write_text('''import hashlib,json,os,secrets,time
from pathlib import Path
duration=DURATION
started=time.monotonic(); rounds=0; payload=b'LocalPilot durable filesystem exercise'*4096
while time.monotonic()-started < duration:
    digest=hashlib.sha256(payload+str(rounds).encode()).hexdigest()
    state={'round':rounds,'elapsed':round(time.monotonic()-started,2),'digest':digest}
    Path('checkpoint.tmp').write_text(json.dumps(state));os.replace('checkpoint.tmp','checkpoint.json')
    assert json.loads(Path('checkpoint.json').read_text())==state
    if rounds % 15 == 0: print(json.dumps(state),flush=True)
    rounds+=1;time.sleep(2)
proof={'elapsed':time.monotonic()-started,'rounds':rounds,'marker':'LONGRUN_PASS::'+secrets.token_hex(12)}
Path('soak-proof.json').write_text(json.dumps(proof));print(json.dumps(proof),flush=True)
'''.replace('DURATION',str(args.soak_seconds)))
            soak=await call(client,'run_shell',workspace='project',command='python3 soak.py',timeout_seconds=args.soak_seconds+180,request_id='one-hour-soak')
            soak_meta={'job_id':soak['job_id'],'root':str(root),'config':str(config),'seconds':args.soak_seconds,
                       'started_at':soak['started_at'],'expected_end':soak['started_at']+args.soak_seconds,
                       'script_sha256':hashlib.sha256((work/'soak.py').read_bytes()).hexdigest()}
            Path(args.output).with_name('soak-session.json').write_text(json.dumps(soak_meta,indent=2)+'\n')
    report={'checked_at':time.time(),'root':str(root),'checks':checks,'passed':sum(x['passed'] for x in checks),'total':len(checks)}
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':report['passed'],'total':report['total'],'root':str(root)},indent=2))
    assert report['passed']==report['total'],[c['name'] for c in checks if not c['passed']]


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--soak-seconds',type=int,default=0);parser.add_argument('--edges',action='store_true')
    asyncio.run(main(parser.parse_args()))
