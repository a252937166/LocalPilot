"""Protocol-level integration checks; touches only temporary test directories."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import socket
import sys
import tempfile
import time
from anyio import fail_after
from mcp import Client, StdioServerParameters


async def verify():
    source = Path(__file__).resolve().parents[1]
    checks=[]
    def check(name, condition, evidence=None):
        checks.append({'name':name,'passed':bool(condition),'evidence':evidence})
    def content(result):
        return result.structured_content or {}
    async def call(client,name,**arguments):
        result=await client.call_tool(name,arguments)
        if result.is_error:
            raise AssertionError(f'{name}: {[getattr(item,"text","") for item in result.content]}')
        return content(result)
    async def reject(client,name,**arguments):
        result=await client.call_tool(name,arguments)
        return result.is_error
    async def finished(client,job):
        deadline=time.monotonic()+15
        while job['status'] in ('running','starting'):
            if time.monotonic()>deadline:
                raise AssertionError('job did not finish')
            await asyncio.sleep(0.1)
            job=await call(client,'job_status',job_id=job['job_id'])
        return job

    with tempfile.TemporaryDirectory(prefix='localpilot-v2-') as temporary:
        root=Path(temporary).resolve()
        workspace=root/'workspace'; workspace.mkdir()
        state=root/'state'
        outside=root/'outside.txt'; outside.write_text('PRIVATE_SENTINEL')
        (workspace/'.env').write_text('SECRET_SENTINEL')
        (workspace/'escape').symlink_to(outside)
        (workspace/'escape-dir').symlink_to(root)
        config=root/'config.json'
        config.write_text(json.dumps({'device_label':'LocalPilot integration','workspaces':{'project':str(workspace)},
                                     'state_dir':str(state),'shell_enabled':True,'shell_network':False,'max_file_bytes':2097152}))
        parameters=StdioServerParameters(command=sys.executable,args=['-I',str(source/'agent/server.py')],
                                        env={'LOCALPILOT_CONFIG':str(config),'LOCALPILOT_TEST_SECRET':'NEVER_INHERIT_THIS'})
        with fail_after(90):
            async with Client(parameters) as client:
                tools=(await client.list_tools()).tools
                check('file_shell_tools_discovered',{'device_status','list_directory','read_file','search_files','write_file','replace_text','run_shell','job_status','cancel_job'} <= {tool.name for tool in tools},[tool.name for tool in tools])
                annotations={tool.name:tool.annotations.read_only_hint for tool in tools}
                check('mutation_annotations',not annotations['write_file'] and not annotations['replace_text'] and not annotations['run_shell'] and not annotations['cancel_job'])
                info=await call(client,'device_status')
                check('actual_workspace',info['workspaces']=={'project':str(workspace)},info)
                check('unknown_workspace_rejected',await reject(client,'list_directory',workspace='other'))
                check('parent_escape_rejected',await reject(client,'read_file',workspace='project',path='../outside.txt'))
                check('absolute_escape_rejected',await reject(client,'read_file',workspace='project',path=str(outside)))
                check('symlink_file_rejected',await reject(client,'read_file',workspace='project',path='escape'))
                check('symlink_directory_rejected',await reject(client,'write_file',workspace='project',path='escape-dir/outside.txt',content='BAD'))
                check('credential_file_rejected',await reject(client,'read_file',workspace='project',path='.env'))
                new=await call(client,'write_file',workspace='project',path='demo/note.txt',content='阶段一\nHello LocalPilot\n',create_parents=True)
                check('create_and_disk_readback',(workspace/'demo/note.txt').read_text()=='阶段一\nHello LocalPilot\n',new)
                read=await call(client,'read_file',workspace='project',path='demo/note.txt',max_chars=3)
                tail=await call(client,'read_file',workspace='project',path='demo/note.txt',offset=read['next_offset'])
                check('unicode_pagination',read['content']+tail['content']==(workspace/'demo/note.txt').read_text())
                check('unconditional_overwrite_rejected',await reject(client,'write_file',workspace='project',path='demo/note.txt',content='BAD'))
                changed=await call(client,'replace_text',workspace='project',path='demo/note.txt',old_text='阶段一',new_text='阶段二',expected_sha256=new['sha256'])
                check('patch_and_disk_readback',(workspace/'demo/note.txt').read_text().startswith('阶段二'),changed)
                check('stale_hash_rejected',await reject(client,'write_file',workspace='project',path='demo/note.txt',content='BAD',expected_sha256=new['sha256']))
                matches=await call(client,'search_files',workspace='project',pattern='*.txt',query='阶段二')
                check('content_search',any(item['path']=='demo/note.txt' for item in matches['results']),matches)
                listing=await call(client,'list_directory',workspace='project',path='demo')
                check('directory_listing',any(item['name']=='note.txt' for item in listing['entries']))
                shell=await finished(client,await call(client,'run_shell',workspace='project',command='printf shell-ok > shell.txt; cat shell.txt',request_id='one-shell'))
                check('real_shell_write_and_readback',shell['exit_code']==0 and shell['stdout']=='shell-ok' and (workspace/'shell.txt').read_text()=='shell-ok',shell)
                same=await call(client,'run_shell',workspace='project',command='printf shell-ok > shell.txt; cat shell.txt',request_id='one-shell')
                check('idempotent_request_id',same['job_id']==shell['job_id'])
                check('request_id_conflict_rejected',await reject(client,'run_shell',workspace='project',command='echo DIFFERENT',request_id='one-shell'))
                envjob=await finished(client,await call(client,'run_shell',workspace='project',command='printf "%s" "${LOCALPILOT_TEST_SECRET-unset}"'))
                check('parent_secrets_not_inherited',envjob['stdout']=='unset',envjob['stdout'])
                denied=await finished(client,await call(client,'run_shell',workspace='project',command='cat '+shlex.quote(str(outside))))
                check('shell_outside_read_denied',denied['exit_code']!=0 and 'PRIVATE_SENTINEL' not in denied['stdout'],denied)
                deniedwrite=await finished(client,await call(client,'run_shell',workspace='project',command='printf BAD > '+shlex.quote(str(outside))))
                check('shell_outside_write_denied',deniedwrite['exit_code']!=0 and outside.read_text()=='PRIVATE_SENTINEL',deniedwrite['stderr'])
                deniedenv=await finished(client,await call(client,'run_shell',workspace='project',command='cat .env'))
                check('shell_credential_file_denied',deniedenv['exit_code']!=0 and 'SECRET_SENTINEL' not in deniedenv['stdout'],deniedenv)
                deniedlink=await finished(client,await call(client,'run_shell',workspace='project',command='cat escape'))
                check('shell_symlink_escape_denied',deniedlink['exit_code']!=0 and 'PRIVATE_SENTINEL' not in deniedlink['stdout'])
                deniedconfig=await finished(client,await call(client,'run_shell',workspace='project',command='cat '+shlex.quote(str(config))))
                check('shell_config_read_denied',deniedconfig['exit_code']!=0 and 'workspaces' not in deniedconfig['stdout'])
                timeout=await finished(client,await call(client,'run_shell',workspace='project',command='sleep 10',timeout_seconds=1))
                check('job_timeout',timeout['status']=='timed_out',timeout)
                running=await call(client,'run_shell',workspace='project',command='sleep 10')
                stopped=await call(client,'cancel_job',job_id=running['job_id'])
                check('job_cancellation',stopped['status']=='cancelled',stopped)
                large=await finished(client,await call(client,'run_shell',workspace='project',command='yes X | head -c 200000'))
                check('bounded_output',large['output_truncated'] and len(large['stdout'].encode())<=65536,len(large['stdout']))
                with socket.socket() as listener:
                    listener.bind(('127.0.0.1',0)); listener.listen(1)
                    port=listener.getsockname()[1]
                    probe=f'import socket; s=socket.socket(); print(s.connect_ex(("127.0.0.1",{port})))'
                    command=shlex.join([str(Path(sys._base_executable).resolve()),'-I','-S','-c',probe])
                    network=await finished(client,await call(client,'run_shell',workspace='project',command=command,timeout_seconds=3))
                check('network_socket_denied',network['stdout'].strip() in ('1','13') or 'PermissionError' in network['stderr'],network)
                protocol=client.protocol_version
            async with Client(parameters) as restarted:
                retained=await call(restarted,'job_status',job_id=shell['job_id'])
                check('job_receipt_survives_restart',retained['stdout']=='shell-ok' and retained['status']=='completed')
            db=sqlite3.connect(state/'localpilot.sqlite3')
            durable=db.execute('SELECT tool,details FROM events WHERE id=?',(changed['receipt_id'],)).fetchone()
            check('durable_file_receipt',durable is not None and json.loads(durable[1])['sha256']==hashlib.sha256((workspace/'demo/note.txt').read_bytes()).hexdigest())
            db.close()
    return {'checked_at':datetime.now(timezone.utc).isoformat(),'scope':'Local MCP stdio, disposable files, macOS sandbox; no ChatGPT or Tunnel',
            'protocol':protocol,'passed':sum(item['passed'] for item in checks),'total':len(checks),'checks':checks}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--output',type=Path); args=parser.parse_args()
    report=asyncio.run(verify())
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(f"PASS {report['passed']}/{report['total']}")
    for item in report['checks']:
        if not item['passed']: print('FAIL',item['name'],json.dumps(item['evidence'],ensure_ascii=False))
    raise SystemExit(0 if report['passed']==report['total'] else 1)
