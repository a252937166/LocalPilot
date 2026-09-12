"""Independent failure-path regressions; temporary files and fixture MCP only."""
from __future__ import annotations
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(os.environ.get('LOCALPILOT_VERIFY_AGENT_ROOT', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / 'agent'))
from PIL import Image
from storage import Storage
from filesystem import Files
from harness import Harness
from image_editor import ImageEditor
from image_handoff import ImageHandoff
from image_workflow import ImageWorkflow
from mcp_bridge import Bridge
import images as image_module

FIXTURE = '''import os,time
from mcp.server import MCPServer
mcp=MCPServer('audit-fixture')
@mcp.tool()
def identify(delay:float=0)->dict:
    time.sleep(delay)
    return {'label':os.environ.get('AUDIT_LABEL','unset')}
if __name__=='__main__':mcp.run()
'''


def verify(output):
    checks = []
    def check(name, passed, evidence=None):
        checks.append({'name':name,'passed':bool(passed),'evidence':evidence})
    with tempfile.TemporaryDirectory(prefix='localpilot-audit-') as directory:
        base=Path(directory);work=base/'work';state=base/'state'
        work.mkdir();state.mkdir()
        storage=Storage(str(state))
        settings={'workspaces':{'project':str(work)},'max_file_bytes':2*1024*1024,
                  'permission_mode':'workspace','protected_paths':[], 'state_dir':str(state)}
        files=Files(settings,storage)
        class Jobs:
            settings={'durable_jobs':False}
            def run(self,**kwargs):raise AssertionError('No shell in this suite')
            def status(self,*args):raise AssertionError('No jobs in this suite')
            cancel=status
        harness=Harness(files,Jobs(),storage)
        editor=ImageEditor(files);handoff=ImageHandoff(files,harness,editor);flow=ImageWorkflow(harness,handoff)
        try:
            result=flow.prepare('project','new/nested/output.png','Save an image into a new directory.',
                                'A blue circle','nested-path',create_parents=True)
            ok=bool(result.structured_content.get('task_id')) and not (work/'new').exists()
        except Exception as exc:ok=False
        check('image_prepare_accepts_new_parent_without_creating_it',ok)
        for path in ('missing/out.png','blocked/out.png'):
            if path.startswith('blocked'):(work/'blocked').write_text('ordinary file')
            try:handoff.preflight('project',path,'deny-'+path,create_parents=path.startswith('blocked'));denied=False
            except Exception:denied=True
            check('image_preflight_rejects_'+path.split('/')[0],denied)

        task=harness.create('project','Record an unexpected operation failure without locking the task.',
                            [{'id':'one','step':'Write','status':'in_progress'}],
                            [{'kind':'file_equals','path':'fault.txt','value':'ok'}])
        calls=[]
        def broken(**kwargs):calls.append(1);raise RuntimeError('fixture operation failed unexpectedly')
        harness.operations['write_file']=broken
        try:harness.execute(task['task_id'],'one','write_file',{'path':'fault.txt','content':'ok'},'fault')
        except Exception:pass
        action=harness._actions(task['task_id'])[0];status=harness._load(task['task_id'])['status']
        check('unexpected_failure_is_terminal_and_inspectable',action['status'] in ('failed','interrupted')
              and bool(action['result'].get('error')),{'action_status':action['status'],'task_status':status})
        try:harness.execute(task['task_id'],'one','write_file',{'path':'fault.txt','content':'ok'},'fault')
        except Exception:pass
        check('unexpected_failure_not_reexecuted_on_retry',len(calls)==1)

        image=io.BytesIO();Image.new('RGB',(20,20),'blue').save(image,format='PNG')
        started,release=threading.Event(),threading.Event()
        def slow_fetch(url,**kwargs):
            started.set()
            if not release.wait(10):raise RuntimeError('fixture was not released')
            return image.getvalue(),'fixture.invalid','fixture.invalid'
        editor.fetch_bytes=slow_fetch
        harness.operations['download_image']=editor.download
        task=harness.create('project','Pause while downloading, before a local image is written.',
                            [{'id':'one','step':'Download','status':'in_progress'}],
                            [{'kind':'file_contains','path':'unused.txt','value':'done'}])
        results=[]
        def download():
            try:results.append(harness.execute(task['task_id'],'one','download_image',
                {'path':'paused.png','url':'https://fixture.invalid/image.png'},'download'))
            except Exception:pass
        worker=threading.Thread(target=download);worker.start();assert started.wait(5)
        timer=threading.Timer(.15,release.set);timer.start()
        harness.set_state(task['task_id'],'paused','User requested a pause during download.')
        worker.join(10);timer.join()
        check('pause_prevents_download_from_writing_afterwards',not worker.is_alive() and not (work/'paused.png').exists(),
              {'file_written':(work/'paused.png').exists(),'action_status':harness._actions(task['task_id'])[0]['status']})

        for native,deadline in ((True,False),(False,True)):
            started.clear();release.clear()
            filename='native-paused.png' if native else 'expired.png'
            if native:
                task=harness.prepare_image_save('project',filename,'Stop before saving the original native image.','native-pause')
                operation,step='save_chat_image','save'
                arguments={'path':filename,'file':{'file_id':'fixture','download_url':'https://fixture.invalid/native.png'}}
                harness.operations[operation]=editor.save
            else:
                task=harness.create('project','Do not save a downloaded image after the task deadline.',
                    [{'id':'one','step':'Download','status':'in_progress'}],
                    [{'kind':'file_contains','path':'unused.txt','value':'done'}])
                current=harness._load(task['task_id']);current['deadline']=time.time()+.3;harness._save(current)
                operation,step='download_image','one'
                arguments={'path':filename,'url':'https://fixture.invalid/expired.png'}
            def run_image():
                try:harness.execute(task['task_id'],step,operation,arguments,'image')
                except Exception:pass
            worker=threading.Thread(target=run_image);worker.start();assert started.wait(5)
            timer=threading.Timer(.4 if deadline else .15,release.set);timer.start()
            if not deadline:harness.set_state(task['task_id'],'paused','Pause the native image save.')
            worker.join(10);timer.join()
            check('native_save_honors_pause' if native else 'download_honors_task_deadline',
                  not worker.is_alive() and not (work/filename).exists())

        Image.new('RGB',(20,20),'yellow').save(work/'edit-source.png')
        source_hash=hashlib.sha256((work/'edit-source.png').read_bytes()).hexdigest()
        original_encode=editor.encode;started.clear();release.clear()
        def slow_encode(image,path):
            started.set();assert release.wait(10)
            return original_encode(image,path)
        editor.encode=slow_encode;harness.operations['edit_image']=editor.edit
        task=harness.create('project','Pause a pixel edit before its output commits.',
            [{'id':'edit','step':'Edit','status':'in_progress'}],[{'kind':'file_contains','path':'unused.txt','value':'done'}])
        def edit():
            try:harness.execute(task['task_id'],'edit','edit_image',
                {'path':'edit-source.png','output_path':'edit-paused.png','expected_sha256':source_hash,'rotate':90},'edit')
            except Exception:pass
        worker=threading.Thread(target=edit);worker.start();assert started.wait(5)
        timer=threading.Timer(.15,release.set);timer.start();harness.set_state(task['task_id'],'paused','Pause pixel encoding.')
        worker.join(10);timer.join();editor.encode=original_encode
        check('pixel_edit_honors_pause_and_preserves_source',not (work/'edit-paused.png').exists()
              and hashlib.sha256((work/'edit-source.png').read_bytes()).hexdigest()==source_hash)

        # A preview cache eviction must not destroy a registered edit's reference.
        original_limit=image_module.MAX_CACHE_FILES
        image_module.MAX_CACHE_FILES=1
        try:
            Image.new('RGB',(20,20),'red').save(work/'source.png')
            Image.new('RGB',(20,20),'green').save(work/'other.png')
            reference=handoff.prepare('project','source.png','edited.png','Keep the registered original pixels.','reference-cache')
            reference_id=reference.structured_content['task_id']
            expected=reference.structured_content['source']['image_sha256']
            files.read_image('project','other.png')
            try:
                restored=handoff.result(reference_id)
                actual=hashlib.sha256(base64.b64decode(restored.meta['localpilot/referenceSource']['data'])).hexdigest()
                kept=actual==expected
            except Exception:kept=False
            check('registered_reference_survives_preview_cache_eviction',kept)
            # Reload old metadata without the new durable reference field.
            legacy=harness._load(reference_id);legacy.pop('reference_image_attachment',None);harness._save(legacy)
            files.read_image('project','other.png')
            try:
                restored=handoff.result(reference_id)
                recovered=hashlib.sha256(base64.b64decode(restored.meta['localpilot/referenceSource']['data'])).hexdigest()==expected
            except Exception:recovered=False
            check('legacy_reference_recovers_only_matching_original',recovered)
        finally:image_module.MAX_CACHE_FILES=original_limit

        fixture=base/'fixture.py';fixture.write_text(FIXTURE)
        cfg={'enabled':True,'config_files':[],'servers':{'fixture':{'command':sys.executable,'args':[str(fixture)],'env':{'AUDIT_LABEL':'first'}}},
             'allow':['*'],'deny':[],'idle_seconds':600,'call_timeout_seconds':10,'max_output_chars':4096}
        bridge=Bridge({'mcp_bridge':cfg},storage)
        try:
            def identify():
                value=bridge.call('fixture','identify')
                return (value.get('structured_content') or json.loads(value['text']))['label']
            first=identify()
            bridge.extra['fixture']['env']['AUDIT_LABEL']='second'
            second=identify()
            check('mcp_config_change_reconnects_to_current_target',first=='first' and second=='second',{'first':first,'second':second})
            active_result=[]
            def active_call():active_result.append(bridge.call('fixture','identify',{'delay':.5}))
            caller=threading.Thread(target=active_call);caller.start()
            until=time.monotonic()+5
            while not any(s.get('active') for s in list(bridge.sessions.values())) and time.monotonic()<until:time.sleep(.01)
            bridge.extra['fixture']['env']['AUDIT_LABEL']='third'
            try:identify();rejected=False
            except Exception:rejected=True
            caller.join(10)
            check('config_refresh_does_not_interrupt_active_mcp_call',rejected and not caller.is_alive()
                  and bool(active_result) and not active_result[0]['is_error'])
            check('mcp_refresh_succeeds_after_active_call_finishes',identify()=='third')
            bridge.extra['fixture']['args'] += ['--api-key','audit-private-token']
            bridge.extra['remote']={'url':'https://user:audit-private-password@example.invalid/private-token-path?key=audit-private-query'}
            listing=json.dumps(bridge.list_servers())
            check('mcp_discovery_does_not_expose_configured_secrets',not any(s in listing for s in
                  ('audit-private-token','audit-private-password','audit-private-query','private-token-path')))
            broken_config=base/'bad.json';broken_config.write_text(json.dumps({'mcpServers':['unexpected']}))
            bridge.config_files=[broken_config]
            try:
                catalog=bridge.list_servers();healthy=any(s['name']=='fixture' for s in catalog['servers']) and bool(catalog['errors'])
            except Exception:healthy=False
            check('malformed_mcp_catalog_does_not_hide_valid_servers',healthy)
            bridge.config_files=[];bridge.extra['bad-entry']={'command':sys.executable,'args':7}
            try:
                catalog=bridge.list_servers();healthy=any(s['name']=='fixture' for s in catalog['servers']) and bool(catalog['errors'])
            except Exception:healthy=False
            check('malformed_mcp_entry_is_isolated',healthy)
        finally:bridge.shutdown();storage.db.close()
    report={'source':str(ROOT),'passed':sum(c['passed'] for c in checks),'total':len(checks),'checks':checks}
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))
    return report['passed']==report['total']


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    raise SystemExit(0 if verify(p.parse_args().output) else 1)
