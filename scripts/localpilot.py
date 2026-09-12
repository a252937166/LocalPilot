#!/usr/bin/env python3
"""Local setup, runtime deployment and tunnel management for LocalPilot."""
from __future__ import annotations
import argparse
import getpass
import json
import os
import platform
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
CONFIG = Path.home() / '.config/localpilot/config.json'
RUNTIME = Path.home() / '.local/share/localpilot/runtime'
STATE = Path.home() / '.local/state/localpilot'
KEY = Path.home() / '.config/localpilot/tunnel-runtime.key'
STATUS_FIELDS = ('alias', 'tunnel_id', 'process_running', 'healthy', 'ready', 'health_url', 'ui_url')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('x', encoding='utf-8') as file:
        os.fchmod(file.fileno(), 0o600)
        file.write(json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def init(args):
    root = Path(args.workspace).expanduser().resolve(strict=True)
    if CONFIG.exists():
        print(f'Existing local config preserved: {CONFIG}')
        return
    node = Path(shutil.which('node')).resolve() if shutil.which('node') else None
    save(CONFIG, {'device_label': args.device_label, 'workspaces': {args.workspace_id: str(root)},
                  'state_dir': str(STATE), 'shell_enabled': True, 'shell_network': False,
                  'max_file_bytes': 2097152, 'step_cards': False, 'shell_read_paths': [str(node.parent.parent)] if node else [],
                  'shell_path_entries': [str(node.parent)] if node else []})
    print(f'Config: {CONFIG}')


def install(args):
    if not CONFIG.is_file():
        raise SystemExit('Run init first.')
    RUNTIME.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PROJECT/'agent', RUNTIME/'agent', dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', 'workspace'))
    shutil.copyfile(PROJECT/'requirements.txt', RUNTIME/'requirements.txt')
    python = RUNTIME/'.venv/bin/python'
    uv = shutil.which('uv')
    if uv:
        if not python.exists():
            subprocess.run([uv,'venv',str(RUNTIME/'.venv'),'--python',sys.executable],check=True)
        subprocess.run([uv,'pip','install','--python',str(python),'-r',str(RUNTIME/'requirements.txt')],check=True)
    else:
        if not python.exists():
            subprocess.run([sys.executable,'-m','venv',str(RUNTIME/'.venv')],check=True)
        subprocess.run([str(python),'-m','pip','install','-r',str(RUNTIME/'requirements.txt')],check=True)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    launcher = RUNTIME/'start-agent'
    architecture=subprocess.run([str(python),'-I','-c','import platform; print(platform.machine())'],capture_output=True,text=True,check=True).stdout.strip()
    prefix=['/usr/bin/arch','-'+architecture] if platform.system()=='Darwin' and architecture in ('arm64','x86_64') else []
    launcher.write_text('#!/bin/sh\numask 077\nexport LOCALPILOT_CONFIG='+shlex.quote(str(CONFIG))+'\nexec '+
                        shlex.join([*prefix,str(python),'-I',str(RUNTIME/'agent/server.py')])+
                        ' 2>>'+shlex.quote(str(STATE/'agent.stderr.log'))+'\n')
    launcher.chmod(0o700)
    print(f'Installed runtime: {RUNTIME}')


def save_key(args):
    # Input stays in the local terminal; it is never printed or passed in argv.
    key = getpass.getpass('OpenAI tunnel runtime API key: ').strip()
    if not key.startswith('sk-'):
        raise SystemExit('Expected an OpenAI runtime API key.')
    KEY.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    descriptor=os.open(KEY,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'w') as file:
        os.fchmod(file.fileno(),0o600)
        file.write(key+'\n')
    print(f'Runtime key saved locally: {KEY}')


def connect(args):
    if not KEY.is_file() or not (RUNTIME/'.venv/bin/python').is_file():
        raise SystemExit('Run install-runtime and save-key first.')
    command=str(RUNTIME/'start-agent')
    subprocess.run(['tunnel-client','runtimes','connect','--alias','localpilot',
                    '--tunnel-id',args.tunnel_id,'--profile','localpilot',
                    '--mcp-command',command,'--runtime-api-key','file:'+str(KEY)],check=True)
    deadline=time.monotonic()+30
    while True:
        result=subprocess.run(['tunnel-client','runtimes','status','localpilot','--json'],capture_output=True,text=True,check=True)
        state=json.loads(result.stdout)
        if state.get('process_running') and state.get('healthy') and state.get('ready'):
            print(json.dumps({key:state.get(key) for key in STATUS_FIELDS},indent=2))
            return
        if time.monotonic()>=deadline:
            raise SystemExit('Runtime is not ready. Inspect ~/.local/state/localpilot/agent.stderr.log and tunnel-client runtimes status localpilot.')
        time.sleep(1)


def status(args):
    result = subprocess.run(['tunnel-client', 'runtimes', 'status', 'localpilot', '--json'],
                            capture_output=True, text=True, check=True)
    state = json.loads(result.stdout)
    print(json.dumps({key: state.get(key) for key in STATUS_FIELDS}, indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='action',required=True)
    setup=commands.add_parser('init')
    setup.add_argument('--workspace',required=True)
    setup.add_argument('--workspace-id',default='project')
    setup.add_argument('--device-label',default='My Mac')
    setup.set_defaults(func=init)
    commands.add_parser('install-runtime').set_defaults(func=install)
    commands.add_parser('save-key').set_defaults(func=save_key)
    tunnel=commands.add_parser('connect')
    tunnel.add_argument('--tunnel-id',required=True)
    tunnel.set_defaults(func=connect)
    commands.add_parser('status').set_defaults(func=status)
    commands.add_parser('stop').set_defaults(func=lambda _: subprocess.run(['tunnel-client','runtimes','stop','localpilot'],check=True))
    args=parser.parse_args(); args.func(args)

if __name__=='__main__':
    main()
