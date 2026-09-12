"""Local configuration; file tools protect it, full-account shell follows OS permissions."""
from __future__ import annotations
import json
import re
import os
from pathlib import Path

VERSION = '0.7.5'
DEFAULT_CONFIG = Path.home() / '.config/localpilot/config.json'
DEFAULT_STATE = Path.home() / '.local/state/localpilot'


def load_config() -> dict:
    path = Path(os.environ.get('LOCALPILOT_CONFIG', DEFAULT_CONFIG)).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f'LocalPilot 配置不存在：{path}；请先运行 scripts/localpilot.py init。')
    settings = json.loads(path.read_text(encoding='utf-8'))
    mode = settings.setdefault('permission_mode', 'workspace')
    if mode not in ('workspace', 'full_machine'):
        raise ValueError('permission_mode 必须是 workspace 或 full_machine。')
    roots = settings.get('workspaces', {})
    if not roots or not isinstance(roots, dict):
        raise ValueError('至少配置一个本机 workspace。')
    settings['workspaces'] = {name: str(Path(value).expanduser().resolve(strict=True)) for name, value in roots.items()}
    if mode == 'full_machine':
        settings['workspaces']['machine'] = '/'
    if not all(Path(value).is_dir() for value in settings['workspaces'].values()):
        raise ValueError('workspace 必须是已存在的目录。')
    settings['state_dir'] = str(Path(settings.get('state_dir', DEFAULT_STATE)).expanduser().resolve())
    state = Path(settings['state_dir'])
    for value in settings['workspaces'].values():
        root = Path(value)
        if mode == 'workspace' and (path.is_relative_to(root) or state.is_relative_to(root)):
            raise ValueError('配置和运行状态必须位于所有 workspace 之外。')
        if mode == 'workspace' and (root == Path('/') or root == Path.home()):
            raise ValueError('请选择具体项目目录，不要将 / 或整个用户目录配置为 workspace。')
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    settings['config_path'] = str(path)
    # Keep the controller's own receipts, key and executable outside tool edits.
    settings['protected_paths'] = [str(path.parent), str(state), str(Path(__file__).resolve().parents[1])]
    settings.setdefault('device_label', 'My Mac')
    settings.setdefault('shell_enabled', True)
    settings.setdefault('shell_network', False)
    execution_mode = settings.setdefault('shell_permission_mode', 'restricted')
    if execution_mode not in ('restricted', 'full_account'):
        raise ValueError('shell_permission_mode 必须是 restricted 或 full_account。')
    if execution_mode == 'full_account' and (mode != 'full_machine' or settings['shell_network'] is not True):
        raise ValueError('full_account 需要 full_machine 与 shell_network=true，由本机配置显式启用。')
    extra_env = settings.setdefault('shell_env', {})
    if not isinstance(extra_env, dict) or len(extra_env) > 50 or any(
        not isinstance(k, str) or not k.isidentifier() or not isinstance(v, str) or '\x00' in v
        or k in ('PATH', 'HOME', 'TMPDIR') for k, v in extra_env.items()
    ):
        raise ValueError('shell_env 必须是文本环境变量字典；PATH/HOME/TMPDIR 由执行器管理。')
    # These are discovery roots, not an expansion of filesystem permissions. Defaults cover the
    # skill libraries of Claude Code, Codex and the agents/opencode conventions that exist on this Mac.
    home = Path.home()
    default_roots = [home / '.claude/skills', home / '.agents/skills', home / '.codex/skills',
                     home / '.config/agents/skills', home / '.config/opencode/skills']
    if os.environ.get('CODEX_HOME'):
        default_roots.insert(3, Path(os.environ['CODEX_HOME']).expanduser() / 'skills')
    skill_roots = settings.setdefault('local_skill_roots', [str(p) for p in default_roots if p.is_dir()])
    if not isinstance(skill_roots, list) or len(skill_roots) > 20 or not all(isinstance(p, str) and p for p in skill_roots):
        raise ValueError('local_skill_roots 必须是最多 20 个本机目录路径的列表。')
    settings['local_skill_roots'] = [str(Path(p).expanduser().absolute()) for p in skill_roots]
    # User-level rule files that apply to every task regardless of the target directory.
    global_files = settings.setdefault('global_instruction_files', [str(home / p) for p in
        ('AGENTS.override.md', 'AGENTS.md', '.codex/AGENTS.override.md', '.codex/AGENTS.md', '.claude/CLAUDE.md')])
    if not isinstance(global_files, list) or len(global_files) > 20 or not all(isinstance(p, str) and p for p in global_files):
        raise ValueError('global_instruction_files 必须是最多 20 个本机文件路径的列表。')
    settings['global_instruction_files'] = [str(Path(p).expanduser().absolute()) for p in global_files]
    settings.setdefault('max_file_bytes', 2 * 1024 * 1024)
    settings.setdefault('durable_jobs', mode == 'full_machine')
    settings.setdefault('prevent_idle_sleep', settings['durable_jobs'])
    settings.setdefault('require_final_assessment', mode == 'full_machine')
    settings.setdefault('default_task_minutes', 240)
    settings.setdefault('default_max_actions', 1000)
    settings.setdefault('default_max_continuations', 80)
    settings.setdefault('default_shell_timeout_seconds', 7200)
    settings.setdefault('max_shell_timeout_seconds', 86400)
    for name, maximum in [('default_task_minutes', 1440), ('default_max_actions', 10000),
                          ('default_max_continuations', 500), ('default_shell_timeout_seconds', 86400),
                          ('max_shell_timeout_seconds', 86400)]:
        if type(settings[name]) is not int or not 1 <= settings[name] <= maximum:
            raise ValueError(f'{name} 必须在 1–{maximum} 之间。')
    # Bridge to the user's own local MCP servers (Codex/Claude configs). Off unless the machine is fully authorized.
    bridge = settings.setdefault('mcp_bridge', {})
    if not isinstance(bridge, dict):
        raise ValueError('mcp_bridge 必须是对象。')
    bridge.setdefault('enabled', mode == 'full_machine')
    bridge.setdefault('config_files', [str(home / '.codex/config.toml'), str(home / '.claude.json')])
    bridge.setdefault('servers', {})
    bridge.setdefault('allow', ['*'])
    bridge.setdefault('deny', [])
    bridge.setdefault('idle_seconds', 600)
    bridge.setdefault('call_timeout_seconds', 120)
    bridge.setdefault('max_output_chars', 65536)
    if (not isinstance(bridge['config_files'], list) or not all(isinstance(p, str) for p in bridge['config_files'])
            or not isinstance(bridge['servers'], dict) or not isinstance(bridge['allow'], list) or not isinstance(bridge['deny'], list)
            or type(bridge['idle_seconds']) is not int or not 10 <= bridge['idle_seconds'] <= 86400
            or type(bridge['call_timeout_seconds']) is not int or not 1 <= bridge['call_timeout_seconds'] <= 600
            or type(bridge['max_output_chars']) is not int or not 1000 <= bridge['max_output_chars'] <= 1000000):
        raise ValueError('mcp_bridge 配置不合法：config_files/allow/deny 为列表，servers 为对象，idle_seconds 10–86400，call_timeout_seconds 1–600，max_output_chars 1000–1000000。')
    bridge['config_files'] = [str(Path(p).expanduser().absolute()) for p in bridge['config_files']]
    bridge['enabled'] = bool(bridge['enabled'])
    # Browser control: attach to a Chrome debug port or launch a LocalPilot-owned Chrome with its own profile.
    browser = settings.setdefault('browser', {})
    if not isinstance(browser, dict):
        raise ValueError('browser 必须是对象。')
    browser.setdefault('enabled', mode == 'full_machine')
    browser.setdefault('cdp_port', 9222)
    browser.setdefault('cdp_url', None)
    legacy_profile = home / 'chrome-debug-profile'
    browser.setdefault('profile_dir', str(legacy_profile if legacy_profile.is_dir() else state / 'browser-profile'))
    browser.setdefault('chrome_path', None)
    browser.setdefault('headless', False)
    browser.setdefault('launch_if_missing', True)
    browser.setdefault('navigation_timeout_ms', 30000)
    browser.setdefault('snapshot_max_chars', 12000)
    browser.setdefault('screenshot_max_side', 1600)
    browser.setdefault('window_size', '1440,900')
    browser.setdefault('user_chrome_dir', str(home / 'Library/Application Support/Google/Chrome'))
    if (type(browser['cdp_port']) is not int or not 1024 <= browser['cdp_port'] <= 65535 or not isinstance(browser['profile_dir'], str)
            or type(browser['navigation_timeout_ms']) is not int or not 1000 <= browser['navigation_timeout_ms'] <= 300000
            or type(browser['snapshot_max_chars']) is not int or not 2000 <= browser['snapshot_max_chars'] <= 60000
            or type(browser['screenshot_max_side']) is not int or not 256 <= browser['screenshot_max_side'] <= 4096
            or not isinstance(browser['window_size'], str) or not re.fullmatch(r'\d{3,5},\d{3,5}', browser['window_size'])
            or not isinstance(browser['user_chrome_dir'], str)):
        raise ValueError('browser 配置不合法：cdp_port 1024–65535，navigation_timeout_ms 1000–300000，snapshot_max_chars 2000–60000，screenshot_max_side 256–4096，window_size 形如 1440,900，user_chrome_dir 为路径。')
    browser['profile_dir'] = str(Path(browser['profile_dir']).expanduser().absolute())
    browser['enabled'] = bool(browser['enabled'])
    # When true, run_task_step renders a small inline receipt card in Chat for each action.
    settings['step_cards'] = bool(settings.get('step_cards', False))
    return settings
