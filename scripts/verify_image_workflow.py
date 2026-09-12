"""Real MCP checks for bounded image workflow messages and disk receipts; host image generation is not simulated as an acceptance pass."""
from __future__ import annotations
import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
from PIL import Image
from mcp import Client, StdioServerParameters


async def verify(output):
    source = Path(os.environ.get('LOCALPILOT_VERIFY_AGENT_ROOT', Path(__file__).resolve().parents[1])).absolute()
    checks = []
    def check(name, passed):
        checks.append({'name': name, 'passed': bool(passed)})
        assert passed, name
    async def call(client, name, **args):
        result = await client.call_tool(name, args)
        assert not result.is_error, (name, result.content)
        return result
    async def reject(client, name, **args):
        return (await client.call_tool(name, args)).is_error
    with tempfile.TemporaryDirectory(prefix='localpilot-reference-') as folder:
        root=Path(folder).resolve();work=root/'work';work.mkdir()
        im=Image.new('RGB',(160,120),'gold');im.paste('navy',(10,10,70,60));im.save(work/'source.png')
        source_sha=hashlib.sha256((work/'source.png').read_bytes()).hexdigest()
        buf=io.BytesIO();Image.new('RGB',(160,120),'green').save(buf,format='PNG');wrong=buf.getvalue()
        cert=root/'cert.pem';key=root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),
                        '-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],
                       check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        key.chmod(0o600);assets={'/wrong':wrong};hits=[];gates={}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path=self.path.split('?')[0];hits.append(path)
                if path in ('/temporary','/denied') or (path=='/flaky' and hits.count(path)==1):
                    self.send_response(403 if path=='/denied' else 503);self.end_headers();return
                body=assets[path]
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers()
                if path in gates:
                    started, release = gates[path]
                    started.set()
                    if not release.wait(10):return
                try:self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):pass
            def log_message(self,*args):pass
        remote=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(cert,key)
        remote.socket=tls.wrap_socket(remote.socket,server_side=True)
        threading.Thread(target=remote.serve_forever,daemon=True).start()
        config=root/'config.json';config.write_text(json.dumps({'workspaces':{'project':str(work)},'state_dir':str(root/'state')}))
        params=StdioServerParameters(command=sys.executable,args=['-I',str(source/'agent/server.py')],
                                    env={'LOCALPILOT_CONFIG':str(config),'SSL_CERT_FILE':str(cert),'NO_PROXY':'127.0.0.1,localhost'})
        try:
            async with Client(params) as client:
                tools={t.name:t for t in (await client.list_tools()).tools}
                uri=tools['prepare_image_workflow'].meta['ui']['resourceUri']
                check('workflow_component_registered',uri.endswith('image-workflow-v0.6.24-r42.html'))
                resource=await client.read_resource(uri)
                check('component_has_no_external_connections',resource.contents[0].meta['ui']['csp']['connectDomains']==[])
                check('message_tools_are_component_only',all(tools[n].meta['ui']['visibility']==['app'] for n in ['claim_image_workflow_message','report_image_workflow_message']))
                import sqlite3
                def age(tid, seconds):
                    with sqlite3.connect(root/'state/localpilot.sqlite3') as db:
                        t=json.loads(db.execute('SELECT snapshot FROM tasks WHERE id=?',(tid,)).fetchone()[0])
                        t['updated_at']-=seconds
                        for m in t.get('image_workflow',{}).get('messages',[]):
                            m['claimed_at']-=seconds
                            if 'acknowledged_at' in m:m['acknowledged_at']-=seconds
                        db.execute('UPDATE tasks SET snapshot=? WHERE id=?',(json.dumps(t),tid))
                args=dict(workspace='project',output_path='generated.png',objective='Generate an orange sun and save it locally.',
                          generation_prompt='请用原生生图画一个橙色太阳，米白背景。',request_id='native-flow')
                prepared=await call(client,'prepare_image_workflow',**args);ctx=prepared.structured_content;tid=ctx['task_id']
                check('registration_does_not_claim_generation_or_write',ctx['message_count']==0 and not ctx['save_readback_verified'] and not (work/'generated.png').exists() and ctx['generation_completion_observable'] is False)
                check('request_replay_keeps_task',(await call(client,'prepare_image_workflow',**args)).structured_content['task_id']==tid)
                check('request_cannot_change_image_brief',await reject(client,'prepare_image_workflow',**{**args,'generation_prompt':'different'}))
                check('request_cannot_change_output',await reject(client,'prepare_image_workflow',**{**args,'output_path':'other.png'}))
                check('initial_idle_delay_enforced',not (await call(client,'claim_image_workflow_message',task_id=tid)).structured_content['send'])
                age(tid,35)
                claim=(await call(client,'claim_image_workflow_message',task_id=tid)).structured_content
                check('first_message_is_only_the_image_brief',claim['send'] and claim['kind']=='generate' and claim['prompt']==args['generation_prompt'])
                check('duplicate_component_cannot_repeat_message',not (await call(client,'claim_image_workflow_message',task_id=tid)).structured_content['send'])
                check('unknown_message_ack_rejected',await reject(client,'report_image_workflow_message',task_id=tid,token='wrong',status='sent'))
                ack=(await call(client,'report_image_workflow_message',task_id=tid,token=claim['token'],status='sent')).structured_content
                check('message_ack_does_not_complete_task',ack['task_status']=='active' and not ack['save_readback_verified'] and ack['message_count']==1)
                again=(await call(client,'report_image_workflow_message',task_id=tid,token=claim['token'],status='sent')).structured_content
                check('same_ack_is_idempotent',again['revision']==ack['revision'])
                check('ack_cannot_be_rewritten',await reject(client,'report_image_workflow_message',task_id=tid,token=claim['token'],status='send_uncertain'))
                check('save_message_waits_for_generation_delay',not (await call(client,'claim_image_workflow_message',task_id=tid)).structured_content['send'])
                task=(await call(client,'get_task',task_id=tid)).structured_content
                check('generic_panel_cannot_duplicate_workflow',not (await call(client,'claim_task_continuation',task_id=tid,expected_revision=task['revision'])).structured_content['send'])
                age(tid,245)
                save=(await call(client,'claim_image_workflow_message',task_id=tid)).structured_content
                check('second_message_uses_bound_save_and_does_not_request_generation',save['send'] and save['kind']=='save' and 'save_chat_image' in save['prompt'] and str(work/'generated.png') in save['prompt'] and '不要求重新生图' in save['prompt'])
                await call(client,'report_image_workflow_message',task_id=tid,token=save['token'],status='sent')
                age(tid,300)
                check('two_message_limit_is_enforced',not (await call(client,'claim_image_workflow_message',task_id=tid)).structured_content['send'])
                file=dict(file_id='file_test_generated',download_url=f'https://127.0.0.1:{remote.server_port}/wrong')
                result=await call(client,'save_chat_image',workspace='project',path='generated.png',file=file,task_id=tid,step_id='save',action_id='save-image')
                saved=result.structured_content
                check('bound_save_returns_audited_disk_readback',saved['readback']['status']=='verified' and saved['readback']['action_id']=='save-image:readback' and (work/'generated.png').read_bytes()==wrong)
                check('bound_save_returns_actual_disk_pixels',any(c.type=='image' for c in result.content))
                requests_before=len(hits)
                replayed=await call(client,'save_chat_image',workspace='project',path='generated.png',file={**file,'download_url':file['download_url']+'?signature=renewed','file_name':'renamed.png'},task_id=tid,step_id='save',action_id='save-image')
                check('renewed_native_download_url_replays_without_another_fetch',replayed.structured_content['replayed'] and len(hits)==requests_before)
                check('same_action_rejects_a_different_native_file',await reject(client,'save_chat_image',workspace='project',path='generated.png',file={**file,'file_id':'file_other'},task_id=tid,step_id='save',action_id='save-image'))
                legacy_arguments={'path':'generated.png','file':{**file,'mime_type':'','file_name':''},'expected_sha256':None,'create_parents':False}
                legacy_fingerprint=hashlib.sha256(json.dumps(['save','save_chat_image',legacy_arguments],sort_keys=True).encode()).hexdigest()
                with sqlite3.connect(root/'state/localpilot.sqlite3') as db:
                    db.execute('UPDATE task_actions SET fingerprint=? WHERE task_id=? AND action_id=?',(legacy_fingerprint,tid,'save-image'))
                legacy_replay=await call(client,'save_chat_image',workspace='project',path='generated.png',file=file,task_id=tid,step_id='save',action_id='save-image')
                check('preupgrade_action_still_replays_exact_arguments',legacy_replay.structured_content['replayed'] and len(hits)==requests_before)
                current=(await call(client,'get_image_workflow',task_id=tid)).structured_content
                check('file_evidence_is_distinct_from_workflow_completion',current['save_readback_verified'] and current['task_status']=='active')
                task=(await call(client,'get_task',task_id=tid)).structured_content
                plan=[dict(p,status='completed') for p in task['plan']]
                await call(client,'update_plan',task_id=tid,plan=plan,expected_revision=task['revision'])
                finished=(await call(client,'finish_task',task_id=tid,assessment={'scope_summary':'The test fixture file was saved and verified by its actual local image readback.', 'evidence_action_ids':['save-image','save-image:readback'],'remaining_work':[]})).structured_content
                check('completion_requires_real_checks',finished['completed'])
                final=(await call(client,'get_image_workflow',task_id=tid)).structured_content
                check('completion_uses_backend_complete_status',final['task_status']=='complete' and final['next_message'] is None)
                check('complete_task_cannot_send_messages',not (await call(client,'claim_image_workflow_message',task_id=tid)).structured_content['send'])
                (work/'generated.png').write_bytes((work/'source.png').read_bytes())
                replay=(await call(client,'save_chat_image',workspace='project',path='generated.png',file=file,task_id=tid,step_id='save',action_id='save-image')).structured_content
                check('replayed_save_does_not_disguise_concurrent_change',replay['replayed'] and replay['readback']['status']=='changed' and replay['readback']['sha256']==source_sha)
                early=(await call(client,'prepare_image_workflow',**{**args,'request_id':'saved-early','output_path':'early.png'})).structured_content['task_id'];age(early,35)
                first=(await call(client,'claim_image_workflow_message',task_id=early)).structured_content
                await call(client,'report_image_workflow_message',task_id=early,token=first['token'],status='sent')
                early_saved=(await call(client,'save_chat_image',workspace='project',path='early.png',file=file,task_id=early,step_id='save',action_id='save-image')).structured_content
                check('save_result_exposes_required_finalization',early_saved['workflow_finalization']['pending'] and 'finish_task' in early_saved['workflow_finalization']['next_step'])
                age(early,100)
                verify_claim=(await call(client,'claim_image_workflow_message',task_id=early)).structured_content
                check('already_saved_image_gets_verification_only_followup',verify_claim['send'] and verify_claim['kind']=='verify' and '不要求重新生成或保存' in verify_claim['prompt'])
                await call(client,'report_image_workflow_message',task_id=early,token=verify_claim['token'],status='sent')
                check('verification_followup_uses_remaining_message_budget',not (await call(client,'claim_image_workflow_message',task_id=early)).structured_content['send'])
                uncertain=(await call(client,'prepare_image_workflow',**{**args,'request_id':'uncertain','output_path':'uncertain.png'})).structured_content['task_id'];age(uncertain,35)
                c=(await call(client,'claim_image_workflow_message',task_id=uncertain)).structured_content
                failed=(await call(client,'report_image_workflow_message',task_id=uncertain,token=c['token'],status='send_uncertain',detail='Network lost https://secret.test/a file_SECRET')).structured_content
                check('uncertain_send_pauses_without_automatic_retry',failed['task_status']=='paused' and not (await call(client,'claim_image_workflow_message',task_id=uncertain)).structured_content['send'])
                check('uncertain_error_does_not_leak_file_handles','file_SECRET' not in json.dumps(failed) and 'secret.test' not in json.dumps(failed))
                paused=(await call(client,'prepare_image_workflow',**{**args,'request_id':'paused','output_path':'paused.png'})).structured_content['task_id'];age(paused,35)
                await call(client,'set_task_state',task_id=paused,status='paused',reason='User stopped')
                check('user_pause_prevents_generation_message',not (await call(client,'claim_image_workflow_message',task_id=paused)).structured_content['send'])
                quiet=(await call(client,'prepare_image_workflow',**{**args,'request_id':'quiet','output_path':'quiet.png'})).structured_content['task_id'];age(quiet,35)
                await call(client,'get_image_workflow',task_id=quiet)
                check('model_status_query_defers_component_message',not (await call(client,'claim_image_workflow_message',task_id=quiet)).structured_content['send'])
                age(quiet,35);await call(client,'poll_image_workflow',task_id=quiet)
                check('component_poll_does_not_reset_quiet_clock',(await call(client,'claim_image_workflow_message',task_id=quiet)).structured_content['send'])
                (work/'existing.png').write_bytes(wrong)
                check('existing_target_is_rejected_before_generation',await reject(client,'prepare_image_workflow',**{**args,'request_id':'exists','output_path':'existing.png'}))
                check('source_sha_without_source_is_rejected',await reject(client,'prepare_image_workflow',**{**args,'request_id':'bad-source','expected_source_sha256':source_sha}))
                source_result=await call(client,'prepare_image_workflow',**{**args,'request_id':'with-source','source_path':'source.png','output_path':'edit.png','expected_source_sha256':source_sha})
                ref=source_result.structured_content;rid=ref['task_id'];private=source_result.meta['localpilot/referenceSource'];age(rid,35)
                assets['/reference']=base64.b64decode(private['data'])
                check('edit_cannot_generate_before_source_binding',not (await call(client,'claim_image_workflow_message',task_id=rid)).structured_content['send'])
                owner='workflow_owner_1234567890';await call(client,'claim_image_edit_upload',task_id=rid,owner=owner)
                await call(client,'bind_image_edit_reference',task_id=rid,owner=owner,file={**file,'download_url':f'https://127.0.0.1:{remote.server_port}/reference'})
                ready=await call(client,'poll_image_workflow',task_id=rid)
                check('ready_reload_retains_exact_reference_pixels',base64.b64decode(ready.meta['localpilot/referenceSource']['data'])==assets['/reference'])
                age(rid,35)
                check('old_reference_component_cannot_duplicate_generation',not (await call(client,'claim_image_edit_followup',task_id=rid)).structured_content['send'])
                edit_claim=(await call(client,'claim_image_workflow_message',task_id=rid)).structured_content
                check('edit_can_dispatch_after_verified_source',edit_claim['send'])
                check('edit_followup_requests_source_read','read_image' in edit_claim['prompt'] and str(work/'source.png') in edit_claim['prompt'])
                comparison=await call(client,'save_chat_image',workspace='project',path='edit.png',file=file,task_id=rid,step_id='save',action_id='save-image')
                pictures=[c for c in comparison.content if c.type=='image']
                check('visual_review_receives_actual_source_and_saved_output',len(pictures)==2 and base64.b64decode(pictures[0].data)==assets['/reference'] and base64.b64decode(pictures[1].data)==wrong)
                assets['/flaky']=wrong
                recovery=(await call(client,'prepare_image_workflow',**{**args,'request_id':'save-recovery','output_path':'recovery.png'})).structured_content['task_id']
                flaky_file={**file,'download_url':f'https://127.0.0.1:{remote.server_port}/flaky'}
                failure=(await call(client,'save_chat_image',workspace='project',path='recovery.png',file=flaky_file,task_id=recovery,step_id='save',action_id='save-image')).structured_content
                check('temporary_download_failure_is_reported_without_write',failure['action']['status']=='failed' and failure['action']['result']['error_code']=='IMAGE_DOWNLOAD_TEMPORARY' and not (work/'recovery.png').exists())
                binding=failure['save_retry']['binding']
                check('transient_failure_returns_same_task_new_attempt_binding',binding['task_id']==recovery and binding['action_id']=='save-image:2' and failure['save_retry']['same_generated_file_only'])
                second=(await call(client,'save_chat_image',file=flaky_file,**binding)).structured_content
                check('retry_saves_and_reads_back_under_original_task',second['action']['status']=='succeeded' and second['readback']['action_id']=='save-image:2:readback')
                audit=(await call(client,'get_task_activity',task_id=recovery,action_id='save-image')).structured_content
                check('retry_preserves_first_failure_receipt',audit['records'][0]['status']=='failed')
                check('retry_can_satisfy_original_immutable_image_check',(await call(client,'get_image_workflow',task_id=recovery)).structured_content['save_readback_verified'])
                for suffix in ('denied','temporary'):
                    test_id=(await call(client,'prepare_image_workflow',**{**args,'request_id':suffix,'output_path':suffix+'.png'})).structured_content['task_id']
                    next_binding={'workspace':'project','path':suffix+'.png','task_id':test_id,'step_id':'save','action_id':'save-image'}
                    for attempt in range(1,4 if suffix=='temporary' else 2):
                        rejected=(await call(client,'save_chat_image',file={**file,'download_url':f'https://127.0.0.1:{remote.server_port}/'+suffix},**next_binding)).structured_content
                        if rejected['save_retry']['available']:next_binding=rejected['save_retry']['binding']
                    check(suffix+'_does_not_offer_unbounded_retry',not rejected['save_retry']['available'] and not (work/(suffix+'.png')).exists())
                # Exercise pause through the public MCP boundary while real TLS
                # response bytes are still in flight, not a mocked downloader.
                for operation in ('save_chat_image', 'download_image'):
                    path='/slow-'+operation;assets[path]=wrong
                    started,release=threading.Event(),threading.Event();gates[path]=(started,release)
                    target=operation+'-paused.png'
                    if operation=='save_chat_image':
                        paused_id=(await call(client,'prepare_image_workflow',**{**args,'request_id':operation+'-in-flight','output_path':target})).structured_content['task_id']
                    else:
                        paused_id=(await call(client,'create_task',workspace='project',objective='Download a test image; cancellation must prevent late writes.',plan=[{'id':'save','step':'Save test image','status':'in_progress'}],checks=[{'kind':'file_sha256','path':target,'value':hashlib.sha256(wrong).hexdigest()}])).structured_content['task_id']
                    url=f'https://127.0.0.1:{remote.server_port}{path}?signature=PRIVATE_TEST_SIGNATURE'
                    if operation=='save_chat_image':
                        pending=asyncio.create_task(call(client,operation,workspace='project',path=target,file={**file,'download_url':url},task_id=paused_id,step_id='save',action_id='in-flight'))
                    else:
                        pending=asyncio.create_task(call(client,'run_task_step',task_id=paused_id,step_id='save',operation=operation,arguments={'path':target,'url':url},action_id='in-flight'))
                    try:
                        assert await asyncio.to_thread(started.wait,5), operation+' did not reach TLS fixture'
                        pause=asyncio.create_task(call(client,'set_task_state',task_id=paused_id,status='paused',reason='Pause during image transfer'))
                        for _ in range(100):
                            with sqlite3.connect(root/'state/localpilot.sqlite3') as db:
                                stored=json.loads(db.execute('SELECT snapshot FROM tasks WHERE id=?',(paused_id,)).fetchone()[0])
                            if stored['status']=='paused':break
                            await asyncio.sleep(.025)
                        assert stored['status']=='paused', operation+' pause was not persisted'
                        release.set()
                        outcome,pause_result=await asyncio.wait_for(asyncio.gather(pending,pause),10)
                        receipt=outcome.structured_content['action']
                        check(operation+'_mcp_pause_stops_late_disk_commit',receipt['status']=='cancelled' and receipt['result']['error_code']=='IMAGE_CANCELLED' and not (work/target).exists() and pause_result.structured_content['status']=='paused')
                        history=(await call(client,'get_task_activity',task_id=paused_id,action_id='in-flight')).structured_content
                        check(operation+'_cancelled_receipt_is_terminal_and_redacted',history['records'][0]['status']=='cancelled' and history['records'][0]['finished_at'] is not None and 'PRIVATE_TEST_SIGNATURE' not in json.dumps(history))
                    finally:
                        release.set()
                        await asyncio.gather(pending,return_exceptions=True)
                check('source_is_preserved',hashlib.sha256((work/'source.png').read_bytes()).hexdigest()==source_sha)
        finally:
            remote.shutdown();remote.server_close()
    report={'created_at':datetime.now(timezone.utc).isoformat(),'source':str(source),'passed':sum(c['passed'] for c in checks),'total':len(checks),'checks':checks,
            'scope':'Isolated MCP, TLS file fixture and local filesystem; no real ChatGPT generation or message delivery.'}
    Path(output).parent.mkdir(parents=True,exist_ok=True);Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':report['passed'],'total':report['total'],'output':str(output)}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    asyncio.run(verify(args.output))
