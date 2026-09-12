"""Real MCP and isolated HTTPS checks for local reference-file handoff; no model calls."""
from __future__ import annotations
import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
from PIL import Image
from mcp import Client, StdioServerParameters


async def verify(output):
    source = Path(__file__).resolve().parents[1]
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
        key.chmod(0o600);assets={'/wrong':wrong};hits=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path=self.path.split('?')[0];hits.append(path);body=assets[path]
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
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
                check('prepare_renders_reference_component',tools['prepare_image_edit'].meta['ui']['resourceUri'].endswith('image-reference-v0.6.15-r33.html'))
                check('reference_uses_one_standard_template_pointer','openai/outputTemplate' not in tools['prepare_image_edit'].meta)
                check('binding_uses_native_file_input_and_is_app_only',tools['bind_image_edit_reference'].meta['openai/fileParams']==['file'] and tools['bind_image_edit_reference'].meta['ui']['visibility']==['app'])
                uri=tools['prepare_image_edit'].meta['ui']['resourceUri'];resource=await client.read_resource(uri)
                check('component_resource_has_no_external_connections',resource.contents[0].meta['ui']['csp']['connectDomains']==[])
                args=dict(workspace='project',source_path='source.png',output_path='edited.png',objective='Edit this source and preserve it.',request_id='ref-test')
                prepared=await call(client,'prepare_image_edit',**args)
                ctx=prepared.structured_content;task_id=ctx['task_id'];private=prepared.meta['localpilot/referenceSource']
                check('pending_transfers_do_not_request_model_polling',ctx['polling_required'] is False and ctx['next_action']['actor']=='reference_component')
                check('reference_and_save_capabilities_are_distinct',ctx['generation_reference_ready'] is False and ctx['save_tool']=='save_chat_image' and ctx['save_tool_available_on_local_server'] is True and 'native_generation_available' not in ctx)
                assets['/reference']=base64.b64decode(private['data'])
                check('source_is_private_and_hash_matches_payload',hashlib.sha256(assets['/reference']).hexdigest()==private['sha256'] and private['data'] not in json.dumps(ctx))
                check('preparation_has_no_image_write',not (work/'edited.png').exists() and ctx['source_status']=='pending')
                task=(await call(client,'get_task',task_id=task_id)).structured_content
                check('reference_snapshot_carries_task_revision_and_observation_time',ctx['revision']==task['revision'] and ctx['observed_at']>0)
                check('source_preservation_is_immutable_check',any(c['kind']=='file_sha256' and c['value']==source_sha for c in task['checks']))
                check('same_request_reuses_task',(await call(client,'prepare_image_edit',**args)).structured_content['task_id']==task_id)
                check('request_cannot_change_destination',await reject(client,'prepare_image_edit',**{**args,'output_path':'other.png'}))
                check('reference_cannot_overwrite_source',await reject(client,'prepare_image_edit',**{**args,'request_id':'same-path','output_path':'source.png'}))
                (work/'existing.png').write_bytes(wrong)
                check('existing_output_rejected_before_upload',await reject(client,'prepare_image_edit',**{**args,'request_id':'exists','output_path':'existing.png'}))
                check('missing_output_parent_rejected_before_upload',await reject(client,'prepare_image_edit',**{**args,'request_id':'missing-dir','output_path':'missing/result.png'}))
                check('source_outside_workspace_rejected',await reject(client,'prepare_image_edit',**{**args,'request_id':'outside','source_path':'../outside.png'}))
                check('stale_source_checksum_rejected',await reject(client,'prepare_image_edit',**{**args,'request_id':'stale','expected_source_sha256':'0'*64}))
                check('followup_before_binding_not_sent',not (await call(client,'claim_image_edit_followup',task_id=task_id)).structured_content['send'])
                plan=[dict(p,status='in_progress' if p['id']=='save' else 'completed' if p['id']=='reference' else 'pending') for p in task['plan']]
                await call(client,'update_plan',task_id=task_id,plan=plan,expected_revision=task['revision'])
                file=dict(file_id='file_test_reference',download_url=f'https://127.0.0.1:{remote.server_port}/reference')
                check('fake_completed_plan_cannot_skip_reference_binding',await reject(client,'save_chat_image',workspace='project',path='edited.png',file=file,task_id=task_id,step_id='save',action_id='save-image'))
                owner='owner_1234567890123456'
                check('one_upload_reserved',(await call(client,'claim_image_edit_upload',task_id=task_id,owner=owner)).structured_content['claimed'])
                check('duplicate_component_cannot_upload',not (await call(client,'claim_image_edit_upload',task_id=task_id,owner='other_1234567890123456')).structured_content['claimed'])
                check('wrong_upload_owner_rejected',await reject(client,'bind_image_edit_reference',task_id=task_id,owner='other_1234567890123456',file=file))
                check('different_image_bytes_cannot_bind',await reject(client,'bind_image_edit_reference',task_id=task_id,owner=owner,file={**file,'download_url':f'https://127.0.0.1:{remote.server_port}/wrong'}))
                check('wrong_image_leaves_reference_unbound',not (await call(client,'get_image_edit_reference',task_id=task_id)).structured_content['reference_bound'])
                bound=await call(client,'bind_image_edit_reference',task_id=task_id,owner=owner,file=file)
                check('actual_https_reference_hash_verified',bound.structured_content['reference_bound'] and bound.structured_content['source_status']=='ready')
                check('binding_snapshot_is_newer_than_original_tool_output',bound.structured_content['revision']>ctx['revision'] and bound.structured_content['observed_at']>ctx['observed_at'])
                count=len(hits);await call(client,'bind_image_edit_reference',task_id=task_id,owner=owner,file=file)
                check('binding_replay_does_not_download_again',len(hits)==count)
                public=(await call(client,'get_task',task_id=task_id)).structured_content
                check('native_reference_not_leaked_into_public_task',file['file_id'] not in json.dumps(public) and bound.meta['localpilot/referenceSource']['file_id']==file['file_id'])
                check('one_followup_reserved',(await call(client,'claim_image_edit_followup',task_id=task_id)).structured_content['send'])
                check('duplicate_followup_refused',not (await call(client,'claim_image_edit_followup',task_id=task_id)).structured_content['send'])
                await call(client,'report_image_edit_handoff',task_id=task_id,status='sent')
                check('followup_ack_persisted',(await call(client,'get_image_edit_reference',task_id=task_id)).structured_content['followup_status']=='sent')
                check('message_ack_does_not_claim_execution',(await call(client,'get_image_edit_reference',task_id=task_id)).structured_content['followup_confirms_execution'] is False)
                await call(client,'save_chat_image',workspace='project',path='edited.png',file=file,task_id=task_id,step_id='save',action_id='save-image')
                check('save_allowed_after_binding',(work/'edited.png').read_bytes()==assets['/reference'])
                check('prepare_replay_after_save_still_reuses_task',(await call(client,'prepare_image_edit',**args)).structured_content['task_id']==task_id)
                original=(work/'source.png').read_bytes();(work/'source.png').write_bytes(wrong)
                review=(await call(client,'review_task',task_id=task_id)).structured_content
                check('changed_original_fails_completion',any(not c['passed'] and c['check']['kind']=='file_sha256' for c in review['checks']))
                (work/'source.png').write_bytes(original)
                await call(client,'set_task_state',task_id=task_id,status='paused',reason='Test pause')
                check('paused_transfer_cannot_send',await reject(client,'claim_image_edit_followup',task_id=task_id))
            async with Client(params) as client:
                state=(await call(client,'get_image_edit_reference',task_id=task_id)).structured_content
                check('binding_and_ack_survive_restart',state['reference_bound'] and state['followup_status']=='sent' and state['task_status']=='paused')
        finally:remote.shutdown()
    report={'at':datetime.now(timezone.utc).isoformat(),'checks':checks,'passed':sum(c['passed'] for c in checks),'total':len(checks),
            'scope':'Real MCP and isolated HTTPS. This does not test ChatGPT upload APIs or native image editing.'}
    Path(output).parent.mkdir(parents=True,exist_ok=True);Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args();asyncio.run(verify(args.output))
