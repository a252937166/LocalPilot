"""Regression tests for the independent 0.6.28 review. Temporary files and MCP fixtures only."""
from __future__ import annotations
import argparse
import asyncio
import difflib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

SOURCE = Path(os.environ.get('LOCALPILOT_VERIFY_AGENT_ROOT', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(SOURCE/'agent'))
from filesystem import Files
from storage import Storage
from local_skills import LocalSkills
from mcp_bridge import Bridge, CallControl
from mcp import Client, StdioServerParameters

FIXTURE = '''
import asyncio
from pathlib import Path
from mcp.server import MCPServer
mcp = MCPServer('Review regression fixture')
@mcp.tool()
def boom() -> str:
    raise ValueError('EXPECTED_REMOTE_FAILURE')
@mcp.tool()
async def delayed(started: str, output: str, delay: float = 1.5) -> dict:
    Path(started).write_text('started')
    await asyncio.sleep(delay)
    Path(output).write_text('completed')
    return {'wrote': True}
if __name__ == '__main__':
    mcp.run()
'''

async def verify(output):
    checks=[]
    def check(name, value):
        checks.append({'name':name,'passed':bool(value)})
        if not value: raise AssertionError(name)
    def rejects(fn):
        try: fn()
        except Exception: return True
        return False
    with tempfile.TemporaryDirectory(prefix='localpilot-review-fixes-') as temp:
        base=Path(temp).resolve(); work=base/'work'; work.mkdir(); (base/'state').mkdir()
        settings={'workspaces':{'project':str(work)},'max_file_bytes':4096,'local_skill_roots':[], 'global_instruction_files':[],
                  'mcp_bridge':{'enabled':True,'config_files':[],'servers':{},'allow':['*'],'deny':[],
                                'idle_seconds':600,'call_timeout_seconds':120,'max_output_chars':5000}}
        storage=Storage(base/'state'); files=Files(settings,storage)
        def patch(text): return files.apply_patch('project',text)
        f=work/'dupe.txt'; f.write_text('head\nsame\nmiddle\nsame\ntail\n')
        patch('--- a/dupe.txt\n+++ b/dupe.txt\n@@ -4 +4 @@\n-same\n+CHANGED\n')
        check('F1_repeated_context_changes_only_declared_line',f.read_text()=='head\nsame\nmiddle\nCHANGED\ntail\n')
        f=work/'insert.txt'; f.write_text('one\ntwo\nthree\n')
        patch('--- a/insert.txt\n+++ b/insert.txt\n@@ -2,0 +3 @@\n+INSERT\n')
        check('F1_zero_context_inserts_after_declared_line',f.read_text()=='one\ntwo\nINSERT\nthree\n')
        for i,(before,after) in enumerate([('a\nb\nc\nd\n','START\na\nb\nD\nEND\n'),('', 'new\n'),('a\nb\n','b\n')]):
            f=work/f'diff{i}.txt'; f.write_text(before)
            diff=''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile='a/'+f.name,tofile='b/'+f.name,n=0))
            patch(diff)
            check(f'F1_unified_roundtrip_with_offsets_{i}',f.read_text()==after)
        f=work/'crlf.txt'; f.write_bytes(b'one\r\ntwo\r\n')
        patch('--- a/crlf.txt\n+++ b/crlf.txt\n@@ -2 +2 @@\n-two\n+TWO\n')
        check('F1_unified_preserves_crlf',f.read_bytes()==b'one\r\nTWO\r\n')
        patch('--- /dev/null\n+++ b/no-newline.txt\n@@ -0,0 +1 @@\n+single\n\\ No newline at end of file\n')
        check('F1_unified_add_preserves_absent_final_newline',(work/'no-newline.txt').read_bytes()==b'single')
        patch('--- a/no-newline.txt\n+++ b/no-newline.txt\n@@ -1 +1 @@\n-single\n\\ No newline at end of file\n+next\n')
        check('F1_unified_can_add_final_newline',(work/'no-newline.txt').read_bytes()==b'next\n')
        f=work/'counts.txt'; f.write_text('before\n')
        check('F1_wrong_hunk_count_rejected',rejects(lambda:patch('--- a/counts.txt\n+++ b/counts.txt\n@@ -1,2 +1 @@\n-before\n+after\n')) and f.read_text()=='before\n')
        f=work/'delete.txt'; f.write_text('NEW DATA\n')
        check('F2_stale_delete_context_rejected',rejects(lambda:patch('--- a/delete.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-OLD DATA\n')) and f.read_text()=='NEW DATA\n')
        f.write_text('one\ntwo\n')
        check('F2_partial_delete_not_treated_as_whole_file',rejects(lambda:patch('--- a/delete.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-one\n')) and f.exists())
        patch('--- a/delete.txt\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-one\n-two\n')
        check('F2_matching_delete_succeeds',not f.exists())
        f=work/'partial.txt'; f.write_text('before\n')
        p='*** Begin Patch\n*** Update File: partial.txt\n@@\n-before\n+after\n*** Add File: huge.txt\n+'+'x'*4097+'\n*** End Patch\n'
        check('batch_size_checked_before_first_write',rejects(lambda:patch(p)) and f.read_text()=='before\n' and not (work/'huge.txt').exists())
        p='*** Begin Patch\n*** Update File: partial.txt\n@@\n-before\n+after\n*** Update File: ./partial.txt\n@@\n-before\n+other\n*** End Patch\n'
        check('path_aliases_rejected_before_first_write',rejects(lambda:patch(p)) and f.read_text()=='before\n')

        package=work/'skills/manual'; package.mkdir(parents=True)
        (package/'SKILL.md').write_text('---\nname: manual-review\ndescription: Trigger on review-trigger.\n---\nUse `python3` on `sample.py` after reading `SKILL.md`.\n')
        policy=package/'agents/openai.yaml'; policy.parent.mkdir(); policy.write_text('policy:\n  allow_implicit_invocation: true\n')
        skills=LocalSkills(files,settings)
        check('F9_initial_policy_read',skills.context('project')['skills'][0]['allow_implicit_invocation'])
        policy.write_text('policy:\n  allow_implicit_invocation: false\n')
        check('F9_changed_policy_invalidates_cache',not skills.context('project')['skills'][0]['allow_implicit_invocation'])
        result=skills.find('project','review-trigger')
        check('F8_implicit_disabled_not_recommended',not result['strong_matches'] and result['recommended_skill_path'] is None)
        check('F8_explicit_name_still_selectable','manual-review' in skills.find('project','用 manual-review')['strong_matches'])
        check('F8_index_preserves_manual_flag',skills.index('project')['skills'][0]['allow_implicit_invocation'] is False)
        policy.unlink()
        check('F9_policy_delete_invalidates_cache',skills.context('project')['skills'][0]['allow_implicit_invocation'])
        (work/'skills/group').mkdir(); skills.context('project')
        nested=work/'skills/group/new'; nested.mkdir(); (nested/'SKILL.md').write_text('---\nname: nested-new\ndescription: Nested example.\n---\nbody\n')
        check('F9_nested_skill_add_invalidates_cache','nested-new' in [s['name'] for s in skills.context('project')['skills']])
        check('F7_filenames_not_missing_commands',skills.read('project',str(package/'SKILL.md'))['readiness']['ready'] is not False)
        (package/'SKILL.md').write_text('---\nname: manual-review\ndescription: Trigger on review-trigger.\n---\nRun `python3 --version`. Parameters: `templateId`, `grant`, `modify`, `SKILL.md`.\n')
        ready=skills.read('project',str(package/'SKILL.md'))['readiness']
        check('F7_inline_executable_only',ready['ready'] is True and [c['name'] for c in ready['commands']]==['python3'])
        (package/'SKILL.md').write_text('---\nname: manual-review\ndescription: Trigger on review-trigger.\nrequires:\n  commands: [no-such-review-command]\n---\nbody\n')
        check('F7_declared_missing_dependency_reported',skills.read('project',str(package/'SKILL.md'))['readiness']['ready'] is False)
        noisy=work/'skills/noisy'; noisy.mkdir(); (noisy/'SKILL.md').write_text('---\nname: booksource-generator\ndescription: 触发词：这个、一下、帮我、书源。\n---\nbody\n')
        check('F6_generic_chinese_not_strong',not skills.find('project','帮我修复这个 Python 文件的错误')['strong_matches'])
        check('F6_domain_trigger_still_strong','booksource-generator' in skills.find('project','制作书源')['strong_matches'])
        check('F6_substrings_inside_identifiers_not_named',not skills.find('project','变量 booksource-generator_count')['strong_matches'])

        bridge=Bridge(settings,storage); marker=base/'late.txt'
        async def late():
            await asyncio.sleep(0.15); marker.write_text('unexpected')
        check('F5_outer_timeout_raises',rejects(lambda:bridge._run(late(),0.02)))
        await asyncio.sleep(0.2)
        check('F5_outer_timeout_cancels_underlying_work',not marker.exists())
        control=CallControl(); control.cancel()
        check('F3_cancel_before_dispatch_prevents_start',rejects(lambda:bridge._run(late(),1,control)) and not marker.exists())
        bridge.shutdown(); bridge.loop.call_soon_threadsafe(bridge.loop.stop)

        fixture=base/'fixture.py'; fixture.write_text(FIXTURE)
        config=base/'config.json'; live_state=base/'protocol-state'
        config.write_text(json.dumps({'workspaces':{'project':str(work)},'state_dir':str(live_state),
                    'local_skill_roots':[],'global_instruction_files':[],'shell_enabled':False,
                    'mcp_bridge':{**settings['mcp_bridge'],'servers':{'fixture':{'command':sys.executable,'args':[str(fixture)]}}}}))
        params=StdioServerParameters(command=sys.executable,args=['-I',str(SOURCE/'agent/server.py')],env={'LOCALPILOT_CONFIG':str(config)})
        async with Client(params) as client:
            async def call(name,**args):
                r=await client.call_tool(name,args)
                if r.is_error: raise RuntimeError((name,r.content))
                return r.structured_content
            async def task(label):
                return await call('create_task',workspace='project',objective=label,
                    plan=[{'id':'one','step':'Fixture call','status':'in_progress'}],
                    checks=[{'kind':'file_contains','path':'partial.txt','value':'before'}])
            t=await task('Remote error receipt')
            failed=await call('run_task_step',task_id=t['task_id'],step_id='one',operation='call_mcp_tool',arguments={'server':'fixture','tool':'boom'},action_id='failure')
            activity=await call('get_task_activity',task_id=t['task_id'])
            check('F4_remote_error_marks_action_failed',failed['action']['status']=='failed' and failed['action']['result']['is_error'])
            check('F4_error_activity_and_step_stats_match',activity['records'][0]['outcome']=='failed' and failed['task']['step_stats']['one']['failed']==1)
            output.parent.mkdir(parents=True,exist_ok=True)
            (output.parent/'fixed-error-panel-fixture.json').write_text(json.dumps({'task':failed['task'],'activity':activity},ensure_ascii=False,indent=2)+'\n')
            async def delayed_call(t, name, delay=1.5):
                return await call('run_task_step',task_id=t['task_id'],step_id='one',operation='call_mcp_tool',
                    arguments={'server':'fixture','tool':'delayed','timeout_seconds':5,
                               'arguments':{'started':str(base/(name+'-started')),'output':str(work/(name+'.txt')),'delay':delay}},action_id=name)
            async def wait_started(name):
                for _ in range(400):
                    if (base/(name+'-started')).exists(): return
                    await asyncio.sleep(0.01)
                raise AssertionError('Fixture did not start: '+name)
            a=await task('Pause one task'); b=await task('Keep other task running')
            a_call=asyncio.create_task(delayed_call(a,'paused',2)); b_call=asyncio.create_task(delayed_call(b,'unrelated',2))
            await wait_started('paused'); await wait_started('unrelated')
            paused=await call('set_task_state',task_id=a['task_id'],status='paused',reason='Regression test pause')
            a_done=await a_call; b_done=await b_call; await asyncio.sleep(0.1)
            check('F3_pause_confirms_owned_call_stop',paused['mcp_cancellation'][0]['cancellation_confirmed'] and a_done['action']['status']=='cancelled')
            check('F3_no_write_after_pause',not (work/'paused.txt').exists())
            check('F3_other_task_same_server_unaffected',b_done['action']['status']=='succeeded' and (work/'unrelated.txt').read_text()=='completed')
            await call('set_task_state',task_id=a['task_id'],status='active',reason='Resume regression test')
            resumed=await delayed_call(a,'resumed',0.01)
            check('F3_resume_reconnects_after_cancellation',resumed['action']['status']=='succeeded' and (work/'resumed.txt').exists())
            c=await task('Cancel task')
            pending=asyncio.create_task(delayed_call(c,'cancelled')); await wait_started('cancelled')
            await call('set_task_state',task_id=c['task_id'],status='cancelled',reason='Cancel regression test'); stopped=await pending
            check('F3_task_cancel_stops_call',stopped['action']['status']=='cancelled' and not (work/'cancelled.txt').exists())
            d=await task('Deadline inside an MCP call')
            # Only this test's temporary database is adjusted, to avoid a minute-long sleep.
            with sqlite3.connect(live_state/'localpilot.sqlite3') as db:
                snapshot=json.loads(db.execute('SELECT snapshot FROM tasks WHERE id=?',(d['task_id'],)).fetchone()[0]); snapshot['deadline']=time.time()+0.3
                db.execute('UPDATE tasks SET snapshot=? WHERE id=?',(json.dumps(snapshot),d['task_id']))
            expired=await delayed_call(d,'deadline')
            check('F3_deadline_caps_startup_and_call',expired['action']['status']=='failed' and expired['action']['result']['error_code']=='MCP_TIMEOUT' and not (work/'deadline.txt').exists())
    result={'scope':'Temporary local file and MCP stdio fixtures, including real cancellation; no external service mutations',
            'checks':checks,'passed':sum(c['passed'] for c in checks),'total':len(checks)}
    output.parent.mkdir(parents=True,exist_ok=True); output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':result['passed'],'total':result['total'],'report':str(output)}))

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--output',type=Path,default=SOURCE/'verification/review-fixes-latest.json')
    asyncio.run(verify(p.parse_args().output))
