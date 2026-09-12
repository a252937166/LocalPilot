"""Bridge the user's locally configured MCP servers (Codex/Claude configs) into Chat, with bounded output."""
from __future__ import annotations
import asyncio
import hashlib
from concurrent.futures import CancelledError as FutureCancelledError
import json
import os
from pathlib import Path
import re
import threading
import time
import tomllib
from urllib.parse import urlsplit

from mcp import Client, StdioServerParameters
from mcp.server.mcpserver.exceptions import ToolError
from image_delivery import MAX_IMAGE_BYTES, validated_preview

NAME = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
STARTUP_TIMEOUT = 45
CANCEL_WAIT = 5


class CallControl:
    """One call's cancellation handle; owned by the harness, never supplied by a model."""
    def __init__(self, scope=''):
        self.scope = scope
        self.requested = threading.Event()
        self.settled = threading.Event()
        self.lock = threading.Lock()
        self.task = self.loop = None
        self.stop_confirmed = False

    def cancel(self):
        with self.lock:
            if self.requested.is_set():
                return
            self.requested.set()
            if self.task is not None and not self.task.done():
                self.loop.call_soon_threadsafe(self.task.cancel)

    def summary(self):
        return {'cancel_requested': self.requested.is_set(), 'cancellation_confirmed': self.stop_confirmed,
                'call_settled': self.settled.is_set()}


class BridgeStopped(ToolError):
    def __init__(self, message, code, control):
        super().__init__(message)
        self.receipt = {'error_code': code, **control.summary()}


def _shrink_image(data_b64, mime_type):
    """Validate pixels and return their actual format with bounded size and dimensions."""
    return validated_preview(data_b64)


def _content_of(content):
    """Split MCP content blocks into text and model-visible images (never placeholders)."""
    parts, images, omitted = [], [], 0
    for block in content or []:
        kind = getattr(block, 'type', None)
        if kind == 'text':
            parts.append(block.text)
        elif kind == 'image':
            if len(images) >= 6:
                omitted += 1
                continue
            try:
                data, mime, size, dims = _shrink_image(getattr(block, 'data', '') or '', getattr(block, 'mime_type', 'image/png'))
                images.append({'data': data, 'mime_type': mime, 'bytes': size, 'width': dims[0] if dims else None, 'height': dims[1] if dims else None})
                parts.append(f'[image {len(images)}: {mime}{f" {dims[0]}x{dims[1]}" if dims else ""}, attached]')
            except Exception as exc:
                parts.append(f'[image could not be decoded: {str(exc)[:80]}]')
        elif kind == 'resource':
            resource = getattr(block, 'resource', None)
            text = getattr(resource, 'text', None)
            parts.append(text if isinstance(text, str) else f'[resource {getattr(resource, "uri", "")}]')
        else:
            parts.append(f'[{kind or "content"}]')
    if omitted:
        parts.append(f'[{omitted} additional images omitted; 6 images attached]')
    return '\n'.join(parts), images


class Bridge:
    def __init__(self, settings, storage):
        cfg = settings['mcp_bridge']
        self.enabled = bool(cfg['enabled'])
        self.config_files = [Path(p) for p in cfg['config_files']]
        self.extra = cfg.get('servers', {})
        self.allow, self.deny = cfg['allow'], cfg['deny']
        self.idle_seconds = cfg['idle_seconds']
        self.call_timeout = cfg['call_timeout_seconds']
        self.max_output = cfg['max_output_chars']
        self.storage = storage
        self.lock = threading.Lock()
        self.loop = None
        self.sessions = {}
        self.connect_locks = {}

    # ---- configuration ------------------------------------------------------------------------
    def catalog(self):
        servers, errors = {}, []
        for path in self.config_files:
            if not path.is_file():
                continue
            try:
                if path.suffix == '.toml':
                    data = tomllib.loads(path.read_text(encoding='utf-8'))
                    source = 'codex'
                else:
                    data = json.loads(path.read_text(encoding='utf-8'))
                    source = 'claude'
                if not isinstance(data, dict):
                    raise ValueError('配置根节点必须是对象。')
                entries = data.get('mcp_servers' if source == 'codex' else 'mcpServers') or {}
                if not isinstance(entries, dict):
                    raise ValueError('MCP 服务器列表必须是名称到配置的对象。')
            except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
                errors.append({'path': str(path), 'error': str(exc)[:200]})
                continue
            for name, raw in entries.items():
                if not isinstance(raw, dict) or not NAME.fullmatch(str(name)):
                    errors.append({'path': str(path), 'error': '存在名称或结构无效的 MCP 服务器配置，已跳过。'})
                    continue
                try:
                    servers.setdefault(name, self._normalize(name, raw, source, str(path)))
                except (ValueError, TypeError):
                    errors.append({'path': str(path), 'server': name, 'error': 'MCP 参数结构无效，已跳过；请检查 args、env、headers 和工具列表。'})
        for name, raw in (self.extra or {}).items():
            if isinstance(raw, dict) and NAME.fullmatch(str(name)):
                try:
                    servers[name] = self._normalize(name, raw, 'localpilot', 'config.json')
                except (ValueError, TypeError):
                    errors.append({'path': 'config.json', 'server': name, 'error': 'MCP 参数结构无效，已跳过。'})
        return servers, errors

    @staticmethod
    def _normalize(name, raw, source, origin):
        for key in ('args', 'enabled_tools', 'disabled_tools'):
            value = raw.get(key)
            if value is not None and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
                raise ValueError('Expected a string list')
        for key in ('env', 'headers', 'http_headers'):
            value = raw.get(key)
            if value is not None and not isinstance(value, dict):
                raise ValueError('Expected an object')
        spec = {'name': name, 'source': source, 'origin': origin, 'enabled': raw.get('enabled', True) is not False,
                'disabled_tools': set(raw.get('disabled_tools') or []), 'enabled_tools': set(raw.get('enabled_tools') or []) or None,
                'env': {k: str(v) for k, v in (raw.get('env') or {}).items()} if isinstance(raw.get('env'), dict) else {}}
        if raw.get('url'):
            spec.update(transport='http', url=str(raw['url']), headers=dict(raw.get('http_headers') or raw.get('headers') or {}),
                        bearer_env=raw.get('bearer_token_env_var'))
        else:
            spec.update(transport='stdio', command=str(raw.get('command') or ''), args=[str(a) for a in (raw.get('args') or [])],
                        cwd=str(raw['cwd']) if raw.get('cwd') else None)
        return spec

    def _allowed(self, name):
        if name in self.deny:
            return False
        return '*' in self.allow or name in self.allow

    def _spec(self, name):
        if not self.enabled:
            raise ToolError('MCP 桥接未启用；在本机配置 mcp_bridge.enabled=true 后重启连接。')
        if not NAME.fullmatch(name or ''):
            raise ToolError('server 名称不合法。')
        servers, _ = self.catalog()
        spec = servers.get(name)
        if spec is None:
            raise ToolError(f'未找到 MCP 服务器 {name}；先用 list_mcp_servers 查看本机配置。')
        if not spec['enabled'] or not self._allowed(name):
            raise ToolError(f'MCP 服务器 {name} 已在本机配置中禁用或不在允许列表中。')
        if spec['transport'] == 'stdio' and not spec['command']:
            raise ToolError(f'MCP 服务器 {name} 缺少 command。')
        return spec

    # ---- event loop thread ---------------------------------------------------------------------
    def _ensure_loop(self):
        with self.lock:
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
                threading.Thread(target=self.loop.run_forever, name='localpilot-mcp-bridge', daemon=True).start()
        return self.loop

    def _run(self, coroutine, timeout, control=None):
        control = control or CallControl()
        async def tracked():
            try:
                with control.lock:
                    control.loop = asyncio.get_running_loop()
                    control.task = asyncio.current_task()
                if control.requested.is_set():
                    coroutine.close()
                    control.stop_confirmed = True  # It never reached a server.
                    raise asyncio.CancelledError()
                return await coroutine
            finally:
                control.settled.set()
        future = asyncio.run_coroutine_threadsafe(tracked(), self._ensure_loop())
        try:
            return future.result(timeout)
        except TimeoutError as exc:
            control.cancel()
            control.settled.wait(CANCEL_WAIT)
            raise BridgeStopped('MCP 调用超时，已请求取消；请核对执行回执后再决定是否重试。', 'MCP_TIMEOUT', control) from exc
        except FutureCancelledError as exc:
            control.settled.wait(CANCEL_WAIT)
            raise BridgeStopped('MCP 调用已取消。' if control.stop_confirmed else 'MCP 调用已请求取消，远端停止尚未确认。',
                                'MCP_CANCELLED', control) from exc

    async def _close(self, name):
        session = self.sessions.pop(name, None)
        if session:
            try:
                await session['client'].__aexit__(None, None, None)
                return True
            except Exception:
                return False
        return True

    async def _sweep(self):
        now = time.monotonic()
        for name in list(self.sessions):
            session = self.sessions.get(name)
            if session and not session.get('active') and now - session['last_used'] > self.idle_seconds:
                await self._close(name)

    async def _session(self, spec, key=None):
        key = key or spec['name']
        async with self.connect_locks.setdefault(key, asyncio.Lock()):
            return await self._connect(spec, key)

    async def _connect(self, spec, key):
        await self._sweep()
        name = spec['name']
        identity = {k: spec.get(k) for k in ('transport', 'command', 'args', 'cwd', 'env', 'url', 'headers', 'bearer_env')}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        session = self.sessions.get(key)
        if session and session.get('config_fingerprint') != fingerprint:
            if session.get('active'):
                raise ToolError(f'MCP 服务器 {name} 的连接配置已改变；已有调用结束前不会切换或中断它，请稍后重试。')
            await self._close(key)
            session = None
        if session:
            session['last_used'] = time.monotonic()
            return session
        if spec['transport'] == 'http':
            if spec.get('bearer_env') or spec.get('headers'):
                raise ToolError(f'MCP 服务器 {name} 需要自定义鉴权头，当前桥接只支持无鉴权的 HTTP 和 stdio 服务器。')
            client = Client(spec['url'])
        else:
            env = dict(spec['env'])
            for env_name in ('NO_PROXY', 'no_proxy'):
                if env_name in os.environ and env_name not in env:
                    env[env_name] = os.environ[env_name]
            client = Client(StdioServerParameters(command=spec['command'], args=spec['args'], env=env, cwd=spec['cwd']))
        try:
            async with asyncio.timeout(STARTUP_TIMEOUT):
                await client.__aenter__()
                tools = (await client.list_tools()).tools
        except asyncio.CancelledError:
            await client.__aexit__(None, None, None)
            raise
        except asyncio.TimeoutError as exc:
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                pass
            raise ToolError(f'MCP 服务器 {name} 在 {STARTUP_TIMEOUT} 秒内没有完成启动或握手。') from exc
        except Exception as exc:
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                pass
            raise ToolError(f'无法连接 MCP 服务器 {name}（{type(exc).__name__}）；请检查本机配置与服务状态。') from exc
        session = {'client': client, 'tools': tools, 'last_used': time.monotonic(), 'connected_at': time.time(),
                   'active': 0, 'server': name, 'config_fingerprint': fingerprint}
        self.sessions[key] = session
        return session

    async def _call(self, spec, tool, arguments, timeout, control):
        # Task-owned sessions isolate cancellation from other tasks using the same server.
        key = spec['name'] + ('::' + control.scope if control.scope else '')
        session = None
        try:
            session = await self._session(spec, key)
            session['active'] += 1
            return await asyncio.wait_for(session['client'].call_tool(tool, arguments), timeout)
        except asyncio.CancelledError:
            closed = await self._close(key)
            control.stop_confirmed = bool(closed and spec['transport'] == 'stdio')
            raise
        except asyncio.TimeoutError:
            closed = await self._close(key)
            control.stop_confirmed = bool(closed and spec['transport'] == 'stdio')
            raise BridgeStopped(f'MCP 工具 {tool} 超过 {timeout} 秒未返回；已结束本地等待。', 'MCP_TIMEOUT', control)
        except ToolError:
            raise
        except Exception as exc:
            await self._close(key)
            raise ToolError(f'MCP 工具调用失败（{type(exc).__name__}）；请检查本机服务状态。') from exc
        finally:
            if session:
                session['active'] = max(0, session['active'] - 1)
                session['last_used'] = time.monotonic()

    # ---- public tools ---------------------------------------------------------------------------
    def list_servers(self):
        servers, errors = self.catalog()
        rows = []
        for name, spec in sorted(servers.items()):
            live = next((s for s in list(self.sessions.values()) if s['server'] == name), None)
            # Discovery does not need launch arguments, URL userinfo, paths or
            # signed queries. These frequently contain configured credentials.
            if spec['transport'] == 'stdio':
                endpoint = spec['command']
                if spec['args']:
                    endpoint += f' ({len(spec["args"])} arguments omitted)'
            else:
                try:
                    parsed = urlsplit(spec['url'])
                    host = parsed.hostname or ''
                    if ':' in host:
                        host = '[' + host + ']'
                    endpoint = parsed.scheme + '://' + host + (f':{parsed.port}' if parsed.port else '')
                except ValueError:
                    endpoint = '(invalid URL; inspect local configuration)'
            rows.append({'name': name, 'source': spec['source'], 'origin': spec['origin'], 'transport': spec['transport'],
                         'command': endpoint[:200],
                         'env_keys': sorted(spec['env'].keys()), 'enabled': spec['enabled'], 'allowed': self._allowed(name),
                         'connected': live is not None, 'tool_count': len(live['tools']) if live else None})
        return {'enabled': self.enabled, 'config_files': [str(p) for p in self.config_files], 'servers': rows,
                'errors': errors, 'note': ('Call list_mcp_tools(server) to connect and see tools, then call_mcp_tool. '
                                           'Servers run locally under the user\'s account with the env recorded in their own config.')}

    def list_tools(self, server):
        spec = self._spec(server)
        session = self._run(self._session(spec), STARTUP_TIMEOUT + 5)
        tools = []
        for tool in session['tools']:
            if tool.name in spec['disabled_tools'] or (spec['enabled_tools'] and tool.name not in spec['enabled_tools']):
                continue
            schema = json.dumps(tool.input_schema, ensure_ascii=False) if getattr(tool, 'input_schema', None) else '{}'
            tools.append({'name': tool.name, 'description': (tool.description or '')[:600],
                          'input_schema': schema[:4000] + ('…' if len(schema) > 4000 else '')})
        return {'server': server, 'transport': spec['transport'], 'tools': tools, 'tool_count': len(tools),
                'connected_at': session['connected_at']}

    def call(self, server, tool, arguments=None, timeout_seconds=None, *, _control=None, _deadline=None):
        spec = self._spec(server)
        if not isinstance(tool, str) or not tool:
            raise ToolError('tool 不能为空。')
        if tool in spec['disabled_tools'] or (spec['enabled_tools'] and tool not in spec['enabled_tools']):
            raise ToolError(f'工具 {tool} 已在本机配置中对服务器 {server} 禁用。')
        arguments = dict(arguments or {})
        timeout = min(int(timeout_seconds or self.call_timeout), 600)
        if timeout < 1:
            raise ToolError('timeout_seconds 必须为正数。')
        started = time.time()
        control = _control or CallControl()
        total_timeout = timeout + STARTUP_TIMEOUT + 5
        if _deadline is not None:
            remaining = _deadline - time.time()
            if remaining <= 0:
                control.cancel(); control.stop_confirmed = True; control.settled.set()
                raise BridgeStopped('MCP 动作的任务时间已到，未开始执行。', 'MCP_TIMEOUT', control)
            total_timeout = min(total_timeout, remaining)
        result = self._run(self._call(spec, tool, arguments, timeout, control), total_timeout, control)
        text, images = _content_of(result.content)
        structured = result.structured_content
        structured_json = json.dumps(structured, ensure_ascii=False) if structured is not None else ''
        truncated = len(text) > self.max_output or len(structured_json) > self.max_output
        payload = {'server': server, 'tool': tool, 'is_error': bool(result.is_error), 'text': text[:self.max_output],
                   'structured_content': structured if len(structured_json) <= self.max_output else {'truncated_json': structured_json[:self.max_output]},
                   'truncated': truncated, 'duration_seconds': round(time.time() - started, 3), 'image_count': len(images)}
        if images:
            payload['images'] = images[:6]
        payload['receipt_id'] = self.storage.event('call_mcp_tool', {
            'server': server, 'tool': tool, 'arguments': json.dumps(arguments, ensure_ascii=False)[:2000],
            'is_error': payload['is_error'], 'text_chars': len(text), 'truncated': truncated, 'images': len(images)})
        return payload

    def shutdown(self):
        if self.loop is None:
            return
        async def close_all():
            for name in list(self.sessions):
                await self._close(name)
        try:
            asyncio.run_coroutine_threadsafe(close_all(), self.loop).result(10)
        except Exception:
            pass
