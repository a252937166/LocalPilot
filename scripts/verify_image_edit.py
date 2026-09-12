"""Actual MCP image edits and HTTPS file imports using a private local TLS fixture."""
from __future__ import annotations
import argparse
import asyncio
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import select
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from PIL import Image, PngImagePlugin
from mcp import Client, StdioServerParameters


async def verify(out):
    project=Path(__file__).resolve().parents[1];checks=[]
    sys.path.insert(0,str(project/'agent'))
    from image_editor import validate_file_url
    def check(name,passed):
        checks.append({'name':name,'passed':bool(passed)});assert passed,name
    async def call(client,name,**args):
        r=await client.call_tool(name,args)
        assert not r.is_error,(name,[getattr(c,'text','') for c in r.content])
        return r.structured_content
    async def reject(client,name,**args):
        r=await client.call_tool(name,args);return r.is_error
    def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
    check('official_root_domain_is_allowed',validate_file_url('https://oaiusercontent.com/image.png')=='oaiusercontent.com')
    check('official_subdomain_is_allowed',validate_file_url('https://files.oaiusercontent.com/image.png')=='files.oaiusercontent.com')
    check('third_party_domain_is_allowed',validate_file_url('https://images.example.test/image.png')=='images.example.test')
    check('custom_https_port_is_allowed',validate_file_url('https://cdn.other.test:8443/image.png')=='cdn.other.test')
    check('https_ip_and_local_domain_are_not_host_filtered',validate_file_url('https://127.0.0.1/image.png')=='127.0.0.1' and validate_file_url('https://localhost/image.png')=='localhost')
    with tempfile.TemporaryDirectory(prefix='localpilot-image-edit-') as tmp:
        root=Path(tmp).resolve();work=root/'work';work.mkdir();state=root/'state'
        config=root/'config/config.json';config.parent.mkdir()
        config.write_text(json.dumps({'workspaces':{'project':str(work)},'state_dir':str(state),'max_file_bytes':2097152}))
        source=work/'source.png';im=Image.new('RGB',(120,80),'red');im.paste('blue',(60,0,120,80))
        pnginfo=PngImagePlugin.PngInfo();pnginfo.add_text('fixture','native-file-byte-preservation');im.save(source,pnginfo=pnginfo)
        initial=digest(source);original=source.read_bytes()
        from filesystem import Files
        from storage import Storage
        edge_state=root/'readback-state';edge_state.mkdir()
        edge_storage=Storage(str(edge_state))
        edge_files=Files({'workspaces':{'project':str(work)},'max_file_bytes':2097152},edge_storage)
        committed=edge_files.write_bytes('project','post-commit.png',original,None,False,tool='save_chat_image')
        Image.new('RGB',(120,80),'green').save(work/'post-commit.png')
        changed=edge_files.images.saved_result(committed,'project','post-commit.png')
        check('concurrent_change_is_not_reported_as_verified',changed.structured_content['wrote_file']
              and changed.structured_content['readback']['status']=='changed'
              and changed.structured_content['readback']['sha256']==digest(work/'post-commit.png')!=committed['sha256'])
        (work/'post-commit.png').unlink()
        missing=edge_files.images.saved_result(committed,'project','post-commit.png')
        check('readback_failure_preserves_successful_write_receipt',not missing.is_error
              and missing.structured_content['wrote_file'] and missing.structured_content['sha256']==committed['sha256']
              and missing.structured_content['readback']['status']=='unavailable'
              and not any(c.type=='image' for c in missing.content) and not (work/'post-commit.png').exists())
        edge_storage.db.close()
        # TLS trust exists only in the isolated MCP process, never in the user's system trust store.
        cert=root/'cert.pem';key=root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1',
                        '-subj','/CN=files.oaiusercontent.com','-addext','subjectAltName=DNS:files.oaiusercontent.com,DNS:images.example.test,DNS:cdn.other.test'],
                       check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        key.chmod(0o600)
        hits=[];requested_hosts=[]
        class ImagesHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path.split('?')[0])
                requested_hosts.append(self.headers.get('Host'))
                if self.path.startswith('/cross-domain-redirect'):
                    self.send_response(302);self.send_header('Location','https://cdn.other.test:8443/image.png');self.end_headers();return
                if self.path.startswith('/redirect'):
                    self.send_response(302);self.send_header('Location','http://127.0.0.1/private');self.end_headers();return
                content=b'not an image' if self.path.startswith('/bad') else original
                self.send_response(200);self.send_header('Content-Type','image/png')
                self.send_header('Content-Length',str(2097153 if self.path.startswith('/large') else len(content)))
                self.end_headers();self.wfile.write(content)
            def log_message(self,*args):pass
        remote=ThreadingHTTPServer(('127.0.0.1',0),ImagesHandler)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key)
        remote.socket=context.wrap_socket(remote.socket,server_side=True)
        threading.Thread(target=remote.serve_forever,daemon=True).start()
        class ProxyHandler(BaseHTTPRequestHandler):
            def do_CONNECT(self):
                if self.path not in {'files.oaiusercontent.com:443','images.example.test:443','cdn.other.test:8443','wrong-cert.example.test:443'}:self.send_error(403);return
                with socket.create_connection(('127.0.0.1',remote.server_port),timeout=3) as upstream:
                    self.send_response(200);self.end_headers();self.wfile.flush()
                    sockets=[self.connection,upstream]
                    while True:
                        readable,_,_=select.select(sockets,[],[],5)
                        if not readable:break
                        for s in readable:
                            data=s.recv(65536)
                            if not data:return
                            (upstream if s is self.connection else self.connection).sendall(data)
            def log_message(self,*args):pass
        proxy=ThreadingHTTPServer(('127.0.0.1',0),ProxyHandler)
        threading.Thread(target=proxy.serve_forever,daemon=True).start()
        env={'LOCALPILOT_CONFIG':str(config),'SSL_CERT_FILE':str(cert),
             'https_proxy':f'http://127.0.0.1:{proxy.server_port}','HTTPS_PROXY':f'http://127.0.0.1:{proxy.server_port}',
             'no_proxy':'','NO_PROXY':''}
        params=StdioServerParameters(command=sys.executable,args=['-I',str(project/'agent/server.py')],env=env)
        try:
            async with Client(params) as client:
                tools={t.name:t for t in (await client.list_tools()).tools}
                preparation_meta=tools['prepare_chat_image_save'].meta or {}
                check('native_save_registration_does_not_require_a_ui_template','openai/outputTemplate' not in preparation_meta and 'resourceUri' not in preparation_meta.get('ui',{}))
                check('image_writes_are_annotated',not tools['edit_image'].annotations.read_only_hint and tools['save_chat_image'].annotations.destructive_hint)
                check('network_annotations_cover_download_tools',tools['download_image'].annotations.open_world_hint and tools['save_chat_image'].annotations.open_world_hint and tools['run_task_step'].annotations.open_world_hint and not tools['run_shell'].annotations.open_world_hint)
                check('url_download_needs_no_native_file_parameter','file_id' not in tools['download_image'].input_schema['properties'] and 'openai/fileParams' not in (tools['download_image'].meta or {}))
                check('native_files_cannot_use_generic_task_wrapper','save_chat_image' not in tools['run_task_step'].input_schema['properties']['operation']['enum'])
                schema=tools['save_chat_image'].input_schema['$defs']['ChatImageFile']
                check('native_file_parameter_schema',tools['save_chat_image'].meta['openai/fileParams']==['file'] and
                      set(schema['required'])=={'download_url','file_id'} and all(schema['properties'][k]['type']=='string' for k in ['download_url','file_id','mime_type','file_name']))
                args={'workspace':'project','path':'source.png','output_path':'rotated.png','expected_sha256':initial,'rotate':90}
                result=await call(client,'edit_image',**args)
                with Image.open(work/'rotated.png') as img:
                    check('clockwise_rotation_pixels',img.size==(80,120) and img.getpixel((40,10))==(255,0,0) and img.getpixel((40,100))==(0,0,255))
                check('new_copy_preserves_source',digest(source)==initial and result['sha256']==digest(work/'rotated.png'))
                await call(client,'edit_image',**{**args,'output_path':'crop.png','crop':[60,0,120,80],'resize':[30,40],'rotate':0})
                with Image.open(work/'crop.png') as img:check('crop_and_resize_pixels',img.size==(30,40) and img.getpixel((15,20))==(0,0,255))
                await call(client,'edit_image',**{**args,'output_path':'mirror.png','rotate':0,'flip':'horizontal'})
                with Image.open(work/'mirror.png') as img:check('horizontal_flip_pixels',img.getpixel((10,10))==(0,0,255))
                await call(client,'edit_image',**{**args,'output_path':'mono.png','rotate':0,'saturation':0})
                with Image.open(work/'mono.png') as img:check('saturation_zero_is_grayscale',len(set(img.getpixel((10,10))))==1)
                await call(client,'edit_image',**{**args,'output_path':'format.jpg','rotate':0})
                with Image.open(work/'format.jpg') as img:check('output_format_matches_extension',img.format=='JPEG')
                for name,extra in [('source_hash',{'expected_sha256':'0'*64}),('unversioned_output',{}),('bad_crop',{'output_path':'bad.png','crop':[0,0,999,30]}),
                                   ('pixel_limit',{'output_path':'bad.png','resize':[12000,12000]}),('bad_factor',{'output_path':'bad.png','brightness':0}),
                                   ('bad_rotation',{'output_path':'bad.png','rotate':45}),('bad_format',{'output_path':'bad.svg'}),('outside',{'output_path':str(root/'outside.png')})]:
                    check('reject_'+name,await reject(client,'edit_image',**{**args,**extra}))
                check('failed_edit_created_no_output',not (work/'bad.png').exists())
                overwritten=await call(client,'edit_image',**{**args,'rotate':180,'output_expected_sha256':result['sha256']})
                check('versioned_output_overwrite',overwritten['previous_sha256']==result['sha256'] and overwritten['sha256']==digest(work/'rotated.png'))
                same=await call(client,'edit_image',**{**args,'output_path':'source.png','rotate':180})
                check('same_path_edit_requires_and_preserves_version',same['previous_sha256']==initial and digest(source)!=initial)
                source.write_bytes(original)
                plan=[{'id':'change','step':'Edit the local picture','status':'in_progress'}]
                task=await call(client,'create_task',workspace='project',objective='Edit a test picture and verify the written file.',plan=plan,
                                checks=[{'kind':'job_succeeded','action_id':'verify'}])
                tid=task['task_id']
                await call(client,'run_task_step',task_id=tid,step_id='change',operation='run_shell',arguments={'command':'true'},action_id='verify')
                await call(client,'run_task_step',task_id=tid,step_id='change',operation='edit_image',arguments={k:v for k,v in {**args,'output_path':'task.png'}.items() if k!='workspace'},action_id='edit')
                check('image_edit_invalidates_old_shell_verification',not (await call(client,'review_task',task_id=tid))['can_complete'])
                action=await call(client,'run_task_step',task_id=tid,step_id='change',operation='edit_image',arguments={k:v for k,v in {**args,'output_path':'task.png'}.items() if k!='workspace'},action_id='edit')
                check('image_edit_action_replay_is_idempotent',action['replayed'] and action['action']['status']=='succeeded')
                check('readonly_step_cannot_edit',await reject(client,'inspect_task_step',task_id=tid,step_id='change',operation='edit_image',arguments={},action_id='no'))
                file={'download_url':'https://files.oaiusercontent.com/image.png?sig=TEST_SIGNED_URL_SENTINEL','file_id':'sediment://file_INTERNAL_TEST_SENTINEL'}
                with sqlite3.connect(state/'localpilot.sqlite3') as db:
                    task_count_before=db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
                saved_result=await client.call_tool('save_chat_image',{'workspace':'project','path':'imported.png','file':file})
                assert not saved_result.is_error, saved_result.content
                saved=saved_result.structured_content
                with sqlite3.connect(state/'localpilot.sqlite3') as db:
                    check('direct_save_does_not_create_task',db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==task_count_before)
                check('direct_save_verifies_persisted_bytes',saved['wrote_file'] and saved['readback']['status']=='verified'
                      and saved['readback']['sha256']==saved['sha256']==digest(work/'imported.png'))
                returned_images=[c for c in saved_result.content if c.type=='image']
                with Image.open(io.BytesIO(base64.b64decode(returned_images[0].data))) as preview:
                    check('direct_save_returns_disk_image_pixels',len(returned_images)==1 and preview.size==(120,80)
                          and preview.getpixel((100,30))==(0,0,255))
                check('direct_image_tools_have_no_component',all('resourceUri' not in (tools[n].meta or {}).get('ui',{})
                      and 'openai/outputTemplate' not in (tools[n].meta or {}) for n in ('read_image','edit_image','save_chat_image')))
                reference_hash=hashlib.sha256(file['file_id'].encode()).hexdigest()
                check('save_receipt_does_not_publish_native_file_handle',saved.get('source_reference_sha256')==reference_hash and file['file_id'] not in json.dumps(saved) and 'file_id' not in saved)
                check('actual_https_download_written',saved['sha256']==digest(work/'imported.png') and saved['width']==120 and '/image.png' in hits)
                check('native_file_bytes_and_metadata_preserved',(work/'imported.png').read_bytes()==original and saved['source_bytes_preserved'] and saved['sha256']==saved['source_sha256'])
                converted=await call(client,'save_chat_image',workspace='project',path='imported.jpg',file=file)
                check('converted_save_verifies_destination_not_download',converted['readback']['status']=='verified'
                      and converted['readback']['sha256']==digest(work/'imported.jpg')!=converted['source_sha256'])
                check('explicit_conversion_changes_bytes',not converted['source_bytes_preserved'] and converted['output_format']=='JPEG' and converted['sha256']!=converted['source_sha256'])
                with Image.open(work/'imported.png') as img:check('downloaded_pixels_preserved',img.getpixel((100,30))==(0,0,255))
                check('save_requires_version_for_existing_target',await reject(client,'save_chat_image',workspace='project',path='imported.png',file=file))
                for name,url,code,kind in [
                    ('path','/mnt/data/PRIVATE_SENTINEL.png','FILE_REFERENCE_NOT_DOWNLOAD_URL','file_path_or_reference'),
                    ('reference','file_PRIVATE_SENTINEL','FILE_REFERENCE_NOT_DOWNLOAD_URL','file_id'),
                    ('empty','','MISSING_DOWNLOAD_URL','empty'),
                    ('inline','data:image/png;base64,PRIVATE_SENTINEL','INLINE_IMAGE_NOT_DOWNLOAD_URL','inline_data'),
                    ('protocol','http://files.oaiusercontent.com/PRIVATE_SENTINEL','DOWNLOAD_URL_REQUIRES_HTTPS','non_https_url'),
                    ('malformed','https://images.example.test:INVALID/PRIVATE_SENTINEL','MALFORMED_DOWNLOAD_URL','invalid_url')]:
                    before=len(hits)
                    failed=await client.call_tool('save_chat_image',{'workspace':'project','path':'rejected.png','file':{**file,'download_url':url}})
                    detail=failed.structured_content
                    check('diagnostic_'+name,failed.is_error and detail['error_code']==code and detail['input_kind']==kind and detail['error_source']=='localpilot' and detail['wrote_file'] is False and 'PRIVATE_SENTINEL' not in json.dumps(detail) and len(hits)==before)
                redirect=await client.call_tool('save_chat_image',{'workspace':'project','path':'rejected.png','file':{**file,'download_url':'https://files.oaiusercontent.com/redirect'}})
                check('redirect_validation_has_distinct_stage',redirect.is_error and redirect.structured_content['error_stage']=='download_redirect')
                for name,url in [('http','http://files.oaiusercontent.com/image.png'),('credentials','https://user:pass@files.oaiusercontent.com/image.png'),
                                 ('bad_certificate','https://wrong-cert.example.test/image.png'),
                                 ('redirect','https://files.oaiusercontent.com/redirect'),('oversize','https://files.oaiusercontent.com/large'),('not_image','https://files.oaiusercontent.com/bad')]:
                    check('reject_download_'+name,await reject(client,'save_chat_image',workspace='project',path='rejected.png',file={**file,'download_url':url}))
                check('rejected_downloads_leave_no_file',not (work/'rejected.png').exists())
                third_url='https://images.example.test/image.png?signature=THIRD_PARTY_SIGNED_URL_SENTINEL'
                third=await call(client,'download_image',workspace='project',path='third-party.png',url=third_url)
                check('third_party_https_download_saved_exact_bytes',third['sha256']==initial and (work/'third-party.png').read_bytes()==original and 'images.example.test' in requested_hosts and 'file_id' not in third)
                via_file=await call(client,'save_chat_image',workspace='project',path='native-third-party.png',file={**file,'download_url':third_url})
                check('native_file_input_also_accepts_other_domains',via_file['sha256']==initial and via_file['download_host']=='images.example.test')
                redirected=await call(client,'download_image',workspace='project',path='redirected.png',url='https://images.example.test/cross-domain-redirect')
                check('cross_domain_redirect_and_custom_port_download',redirected['sha256']==initial and 'cdn.other.test:8443' in requested_hosts and redirected['requested_host']=='images.example.test' and redirected['download_host']=='cdn.other.test')
                check('direct_url_download_keeps_overwrite_protection',await reject(client,'download_image',workspace='project',path='third-party.png',url=third_url))
                before=len(hits)
                check('url_download_respects_workspace_scope',await reject(client,'download_image',workspace='project',path=str(root/'outside.png'),url=third_url) and len(hits)==before)
                baseline=await call(client,'run_task_step',task_id=tid,step_id='change',operation='run_shell',arguments={'command':'true'},action_id='verify:url-baseline')
                await call(client,'inspect_task_step',task_id=tid,step_id='change',operation='job_status',arguments={'job_id':baseline['action']['result']['job_id'],'wait_seconds':20},action_id='observe-url-baseline')
                check('verification_passes_before_url_mutation',(await call(client,'review_task',task_id=tid))['checks'][0]['passed'])
                await call(client,'run_task_step',task_id=tid,step_id='change',operation='download_image',arguments={'path':'task-url.png','url':third_url},action_id='url-download')
                check('url_download_invalidates_old_verification',not (await call(client,'review_task',task_id=tid))['checks'][0]['passed'])
                before=len(hits)
                replay=await call(client,'run_task_step',task_id=tid,step_id='change',operation='download_image',arguments={'path':'task-url.png','url':third_url},action_id='url-download')
                check('task_url_download_is_idempotent',replay['replayed'] and replay['action']['status']=='succeeded' and replay['action']['result']['sha256']==initial and len(hits)==before)
                attached=await call(client,'save_chat_image',workspace='project',path='task-import.png',file=file,task_id=tid,step_id='change',action_id='import')
                check('file_input_can_write_as_task_action',attached['action']['operation']=='save_chat_image' and attached['action']['status']=='succeeded')
                rejected=await call(client,'save_chat_image',workspace='project',path='rejected.png',file={**file,'download_url':'sandbox:/mnt/data/PRIVATE_SENTINEL.png'},task_id=tid,step_id='change',action_id='bad-file-input')
                activity=await call(client,'get_task_activity',task_id=tid,action_id='bad-file-input')
                check('task_and_activity_preserve_error_origin',rejected['action']['status']=='failed' and activity['records'][0]['result']['error_code']=='FILE_REFERENCE_NOT_DOWNLOAD_URL' and activity['records'][0]['result']['error_source']=='localpilot' and 'PRIVATE_SENTINEL' not in json.dumps(activity))
                check('task_workspace_override_rejected',await reject(client,'save_chat_image',workspace='other',path='other.png',file=file,task_id=tid,step_id='change',action_id='wrong'))
                pending_args={'workspace':'project','path':'generated.png','objective':'Generate one picture in ordinary Chat and save that exact image on this Mac.', 'request_id':'native-save-fixture'}
                pending=await call(client,'prepare_chat_image_save',**pending_args)
                pending_id=pending['task_id']
                check('native_save_is_registered_before_generation',pending['auto_continue'] and pending['max_continuations']==3 and pending['image_save']['path']==str(work/'generated.png') and not (work/'generated.png').exists())
                again=await call(client,'prepare_chat_image_save',**pending_args)
                check('save_preparation_replay_keeps_same_task',again['task_id']==pending_id and again['revision']==pending['revision'])
                check('save_preparation_cannot_change_target',await reject(client,'prepare_chat_image_save',**{**pending_args,'path':'different.png'}))
                check('save_preparation_rejects_outside_path',await reject(client,'prepare_chat_image_save',**{**pending_args,'request_id':'outside-save','path':str(root/'outside.png')}))
                check('pending_image_cannot_finish',not (await call(client,'finish_task',task_id=pending_id))['completed'])
                followup=await call(client,'claim_task_continuation',task_id=pending_id,expected_revision=pending['revision'])
                check('initial_checkpoint_is_not_host_generation_availability',pending['initial_image_checkpoint'] and not (await call(client,'get_task',task_id=pending_id))['initial_image_checkpoint'] and 'native_generation_available' not in pending and 'native_generation_handoff_available' not in pending)
                check('native_handoff_preserves_original_request',pending['objective'] in followup['prompt'])
                check('image_continuation_uses_native_save_without_regeneration',followup['send'] and 'save_chat_image' in followup['prompt'] and '不重新生成' in followup['prompt'])
                check('same_image_checkpoint_is_not_sent_twice',not (await call(client,'claim_task_continuation',task_id=pending_id,expected_revision=pending['revision']))['send'])
                check('image_task_cannot_substitute_shell_or_download',await reject(client,'run_task_step',task_id=pending_id,step_id='save',operation='run_shell',arguments={'command':'true'},action_id='wrong-shell') and await reject(client,'run_task_step',task_id=pending_id,step_id='save',operation='download_image',arguments={'path':'generated.png','url':third_url},action_id='wrong-url'))
                check('image_task_rejects_different_save_path',await reject(client,'save_chat_image',workspace='project',path='different.png',file=file,task_id=pending_id,step_id='save',action_id='save-image'))
                imported=await call(client,'save_chat_image',**pending['image_save_binding'],file=file)
                check('structured_binding_saves_without_model_control_instructions',imported['action']['status']=='succeeded' and 'instruction' not in pending and pending['protocol']['task_state_grants_authorization'] is False)
                check('native_save_requires_subsequent_read',not imported['task']['checks_status']['checks'][0]['passed'] and imported['action']['result']['sha256']==initial)
                observed_result=await client.call_tool('inspect_task_step',{'task_id':pending_id,'step_id':'save','operation':'read_image','arguments':{'path':'generated.png'},'action_id':'read-saved-image'})
                assert not observed_result.is_error
                observed=observed_result.structured_content
                pictures=[c for c in observed_result.content if c.type=='image']
                check('readback_keeps_model_visible_pixels_without_an_output_attachment',len(pictures)==1 and pictures[0].annotations.audience==['assistant'] and len(base64.b64decode(pictures[0].data))>0)
                check('readback_envelope_does_not_repeat_native_source_handle','file_INTERNAL_TEST_SENTINEL' not in observed_result.model_dump_json())
                check('native_save_receipt_and_actual_readback_match',observed['task']['checks_status']['checks'][0]['passed'] and observed['task']['checks_status']['checks'][0]['evidence']['read_back_action_id']=='read-saved-image')
                (work/'generated.png').write_bytes((work/'format.jpg').read_bytes())
                check('changed_image_invalidates_acceptance',not (await call(client,'review_task',task_id=pending_id))['checks'][0]['passed'])
                (work/'generated.png').write_bytes(original)
                ready=await call(client,'get_task',task_id=pending_id)
                await call(client,'update_plan',task_id=pending_id,expected_revision=ready['revision'],plan=[{**p,'status':'completed'} for p in ready['plan']])
                complete=await call(client,'finish_task',task_id=pending_id)
                check('verified_image_task_completes_and_stops_continuation',complete['completed'] and not complete['task']['auto_continue'])
                saved_activity=await call(client,'get_task_activity',task_id=pending_id,action_id='save-image')
                for endpoint,value in [('save',imported),('get',ready),('activity',saved_activity),('finish',complete),('review',await call(client,'review_task',task_id=pending_id))]:
                    check('public_'+endpoint+'_keeps_provenance_without_native_attachment','file_INTERNAL_TEST_SENTINEL' not in json.dumps(value) and '"file_id"' not in json.dumps(value) and reference_hash in json.dumps(value))
                with sqlite3.connect(state/'localpilot.sqlite3') as db:
                    private_action=json.loads(db.execute('SELECT snapshot FROM task_actions WHERE task_id=? AND action_id=?',(pending_id,'save-image')).fetchone()[0])
                    old_task=json.loads(db.execute('SELECT snapshot FROM tasks WHERE id=?',(pending_id,)).fetchone()[0])
                    old_task['final_review']['checks'][0]['evidence']['file_id']=file['file_id']
                    db.execute('UPDATE tasks SET snapshot=? WHERE id=?',(json.dumps(old_task),pending_id))
                check('private_audit_preserves_original_native_file_handle',private_action['result']['file_id']==file['file_id'])
                legacy=await call(client,'get_task',task_id=pending_id)
                check('old_task_snapshots_are_sanitized_on_read','file_INTERNAL_TEST_SENTINEL' not in json.dumps(legacy) and legacy['final_review']['checks'][0]['evidence']['source_reference_sha256']==reference_hash)
                stopped=await call(client,'prepare_chat_image_save',**{**pending_args,'request_id':'pause-save','path':'pause.png'})
                paused=await call(client,'set_task_state',task_id=stopped['task_id'],status='paused',reason='User stopped the save.')
                check('paused_image_save_cannot_continue',not paused['auto_continue'] and await reject(client,'claim_task_continuation',task_id=stopped['task_id'],expected_revision=paused['revision']))
                with sqlite3.connect(state/'localpilot.sqlite3') as db:
                    snapshots=[r[0] for r in db.execute('select snapshot from task_actions')] + [r[0] for r in db.execute('select details from events')]
                check('signed_download_url_not_in_audit_or_task_storage',all('TEST_SIGNED_URL_SENTINEL' not in s and 'THIRD_PARTY_SIGNED_URL_SENTINEL' not in s and 'download_url' not in s for s in snapshots))
        finally:
            proxy.shutdown();proxy.server_close();remote.shutdown();remote.server_close()
    report={'checked_at':datetime.now(timezone.utc).isoformat(),'scope':'Real local MCP with isolated HTTPS fixture; not ChatGPT generated-file end-to-end',
            'passed':sum(x['passed'] for x in checks),'total':len(checks),'checks':checks}
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,indent=2)+'\n');print(f"PASS {report['passed']}/{report['total']}")


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=Path('verification/image-edit-v1/local-edit.json'))
    asyncio.run(verify(p.parse_args().output))
