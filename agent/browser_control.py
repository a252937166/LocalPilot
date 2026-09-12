"""Browser control for Chat: attach to a Chrome over CDP (or launch one on the LocalPilot profile), snapshot pages with element
refs, act by ref, read text and take screenshots the model can see. Runs on its own event loop thread; the Chrome
process is independent of the MCP connection, so sessions survive reconnects."""
from __future__ import annotations
import asyncio
import base64
import hashlib
from concurrent.futures import CancelledError as FutureCancelledError
from contextlib import contextmanager
import fcntl
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
import urllib.parse
import urllib.request

from mcp.server.mcpserver.exceptions import ToolError
from mcp_bridge import CallControl, BridgeStopped
from image_delivery import encode_preview, MAX_SOURCE_PIXELS

CHROME_PATHS = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                '/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary',
                '/Applications/Chromium.app/Contents/MacOS/Chromium']
USER_CHROME = Path.home() / 'Library/Application Support/Google/Chrome'
ACTIONS = ('press_key', 'hover', 'select_option', 'check', 'uncheck', 'scroll', 'back', 'forward', 'reload', 'wait_for', 'focus', 'clear')
TAB_ACTIONS = ('list', 'status', 'open', 'close', 'select', 'clone_logins', 'stop')
DIALOG_POLICIES = ('accept', 'dismiss')
REF = re.compile(r'^e\d{1,6}$')
# SQLite stores copied with the backup API (consistent even while Chrome writes), then plain directories.
PROFILE_FILES = ('Cookies', 'Network/Cookies', 'Login Data', 'Login Data For Account', 'Web Data')
PROFILE_DIRS = ('Local Storage',)

# One accessible-name rule shared by snapshots and by the check that a ref still means what the snapshot showed.
NAME_JS = r"""
(el) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const aria = el.getAttribute('aria-label'); if (aria) return clean(aria);
  const by = el.getAttribute('aria-labelledby');
  if (by) { const t = by.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(e => clean(e.innerText || e.textContent)).join(' '); if (t) return t; }
  if (el.id) { try { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) { const t = clean(l.innerText); if (t) return t; } } catch (e) {} }
  const wrap = el.closest('label'); if (wrap && el.tagName !== 'LABEL') { const t = clean(wrap.innerText); if (t) return t.slice(0, 80); }
  for (const a of ['placeholder', 'title', 'alt', 'name']) { const v = el.getAttribute(a); if (v) return clean(v); }
  if (el.tagName === 'INPUT' && ['submit', 'button', 'reset'].includes((el.type || '').toLowerCase()) && el.value) return clean(el.value);
  const t = clean(el.innerText || el.textContent); return t.slice(0, 80);
}
"""

SNAPSHOT_JS = r"""
(opts) => {
  const mode = opts.mode || 'interactive', maxChars = opts.maxChars || 12000, scope = opts.selector ? document.querySelector(opts.selector) : document.body;
  if (!scope) return {error: 'selector not found'};
  const attr = opts.refAttribute;
  const stateKey = Symbol.for(attr);
  const refs = window[stateKey] || (window[stateKey] = {nodes: new WeakMap(), next: opts.start || 0});
  const interactive = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=checkbox],[role=radio],[role=switch],[role=combobox],[role=textbox],[role=searchbox],[role=option],[role=slider],[role=treeitem],[role=gridcell][tabindex],[contenteditable=""],[contenteditable="true"],[contenteditable="plaintext-only"],[tabindex]:not([tabindex="-1"]),[onclick]';
  const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','PATH','HEAD','META','LINK']);
  const out = []; let counter = Math.max(opts.start || 0, refs.next), chars = 0, truncated = false;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    if (!(el instanceof Element)) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return !!(r.width || r.height || el.getClientRects().length);
  };
  const ownText = el => { let t = ''; for (const n of el.childNodes) if (n.nodeType === 3) t += n.textContent; return clean(t); };
  const nameOf = __NAME_FN__;
  const roleOf = el => {
    const r = el.getAttribute('role'); if (r) return r;
    const t = el.tagName.toLowerCase();
    if (t === 'a') return 'link'; if (t === 'button' || t === 'summary') return 'button'; if (t === 'select') return 'combobox'; if (t === 'textarea') return 'textbox';
    if (t === 'input') { const ty = (el.type || 'text').toLowerCase(); return ({checkbox: 'checkbox', radio: 'radio', submit: 'button', button: 'button', reset: 'button', file: 'file', range: 'slider', number: 'spinbutton', search: 'searchbox', password: 'password'})[ty] || 'textbox'; }
    if (/^h[1-6]$/.test(t)) return 'heading'; if (el.isContentEditable) return 'textbox'; return t;
  };
  const push = node => { const cost = JSON.stringify(node).length; if (chars + cost > maxChars) { truncated = true; return false; } chars += cost; out.push(node); return true; };
  const visit = root => {
    for (const el of root.children) {
      if (truncated) return;
      if (skip.has(el.tagName)) continue;
      if (!visible(el)) continue;
      let consumed = false;
      if (el.matches(interactive)) {
        let ref = refs.nodes.get(el);
        if (!ref) { ref = 'e' + (++counter); refs.nodes.set(el, ref); }
        el.setAttribute(attr, ref);
        const node = {ref, role: roleOf(el), name: nameOf(el)};
        const t = el.tagName.toLowerCase();
        if (t === 'input' || t === 'textarea') { if (!['password', 'checkbox', 'radio'].includes(el.type || '') && el.value) node.value = clean(el.value).slice(0, 120); if (el.placeholder && node.name !== clean(el.placeholder)) node.placeholder = clean(el.placeholder).slice(0, 60); }
        if (t === 'select') { node.value = el.value; node.options = [...el.options].slice(0, 12).map(o => o.textContent.trim().slice(0, 30)); }
        if (t === 'a' && el.getAttribute('href')) node.href = el.getAttribute('href').slice(0, 120);
        if (el.disabled || el.getAttribute('aria-disabled') === 'true') node.disabled = true;
        if (el.checked || el.getAttribute('aria-checked') === 'true') node.checked = true;
        if (el.getAttribute('aria-expanded')) node.expanded = el.getAttribute('aria-expanded') === 'true';
        if (el.getAttribute('aria-selected') === 'true') node.selected = true;
        if (el.isContentEditable && !node.value) { const v = clean(el.innerText).slice(0, 120); if (v) node.value = v; }
        if (!push(node)) return;
        consumed = ['a', 'button', 'select', 'textarea', 'input', 'summary'].includes(t) || el.getAttribute('role') === 'button';
      } else if (/^H[1-6]$/.test(el.tagName)) {
        const t = clean(el.innerText).slice(0, 120); if (t && !push({role: 'heading', level: +el.tagName[1], name: t})) return; consumed = true;
      } else if (mode !== 'interactive' && ['P', 'LI', 'TD', 'TH', 'DT', 'DD', 'LABEL', 'LEGEND', 'FIGCAPTION', 'BLOCKQUOTE', 'PRE'].includes(el.tagName)) {
        const t = clean(el.innerText).slice(0, 160); if (t && el.children.length < 4) { if (!push({role: 'text', name: t})) return; consumed = true; }
      } else if (mode === 'full') {
        const t = ownText(el).slice(0, 160); if (t.length > 1) { if (!push({role: 'text', name: t})) return; }
      }
      if (consumed && !(el.tagName === 'A' && el.querySelector(interactive))) continue;
      if (el.shadowRoot) visit(el.shadowRoot);
      visit(el);
    }
  };
  visit(scope);
  refs.next = counter;
  const frames = [...document.querySelectorAll('iframe')].filter(visible).slice(0, 10).map(f => ({src: (f.getAttribute('src') || '').slice(0, 120), name: f.getAttribute('name') || ''}));
  return {title: document.title, url: location.href, nodes: out, truncated, frames, count: counter, listed: out.filter(n => n.ref).length,
          viewport: {width: innerWidth, height: innerHeight, scrollY: Math.round(scrollY), scrollHeight: document.documentElement.scrollHeight}};
}
""".replace('__NAME_FN__', NAME_JS.strip())


def _norm(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip()[:80]


def render_snapshot(data):
    """Compact text tree the model reads: one line per node, refs in brackets."""
    lines = [f"Page: {data.get('title') or '(untitled)'} — {data.get('url')}"]
    for node in data.get('nodes', []):
        role = node.get('role', '')
        if role == 'heading':
            lines.append(f"{'#' * min(int(node.get('level', 2)), 4)} {node.get('name', '')}")
            continue
        if role == 'text':
            lines.append(f"  {node.get('name', '')}")
            continue
        parts = [f"[{node['ref']}] {role}"]
        if node.get('name'):
            parts.append(json.dumps(node['name'], ensure_ascii=False))
        for key in ('value', 'placeholder', 'href'):
            if node.get(key):
                parts.append(f"{key}={json.dumps(node[key], ensure_ascii=False)}")
        if node.get('options'):
            parts.append('options=' + '|'.join(node['options']))
        for flag in ('checked', 'disabled', 'selected'):
            if node.get(flag):
                parts.append(flag)
        if 'expanded' in node:
            parts.append('expanded' if node['expanded'] else 'collapsed')
        lines.append(' '.join(parts))
    if data.get('frames'):
        lines.append('iframes: ' + '; '.join(f"{f['name'] or '-'} {f['src']}" for f in data['frames']))
    if data.get('truncated'):
        lines.append('… snapshot truncated; use selector= or mode=interactive, or scroll and snapshot again.')
    return '\n'.join(lines)


def _encode_image(picture):
    return encode_preview(picture)


def _copy_sqlite(src, dst, checkpoint=lambda: None):
    """Consistent copy of a Chrome database, even while Chrome holds it open."""
    uri = 'file:' + urllib.parse.quote(str(src)) + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as origin, sqlite3.connect(dst) as copy:
        origin.backup(copy, pages=128, progress=lambda *_: checkpoint(), sleep=0.05)


class BrowserControl:
    def __init__(self, settings, storage):
        cfg = settings['browser']
        self.enabled = bool(cfg['enabled'])
        self.port = int(cfg['cdp_port'])
        self.cdp_url = cfg['cdp_url'] or f'http://127.0.0.1:{self.port}'
        self.explicit_cdp_url = bool(cfg['cdp_url'])
        self.profile_dir = Path(cfg['profile_dir'])
        self.chrome_path = cfg.get('chrome_path') or next((p for p in CHROME_PATHS if Path(p).exists()), None)
        self.headless = bool(cfg['headless'])
        self.launch_if_missing = bool(cfg['launch_if_missing'])
        self.nav_timeout = int(cfg['navigation_timeout_ms'])
        self.snapshot_chars = int(cfg['snapshot_max_chars'])
        self.max_side = int(cfg['screenshot_max_side'])
        self.window_size = str(cfg.get('window_size') or '1440,900')
        self.user_chrome = Path(cfg.get('user_chrome_dir') or USER_CHROME).expanduser()
        self.state = Path(settings['state_dir']) / 'browser'
        self.legacy_tab_ids = self.profile_dir.exists() and not (self.state / 'tab-ids.json').exists()
        self.storage = storage
        self.lock = threading.Lock()
        self.loop = None
        self.lifecycle_lock = asyncio.Lock()
        self.registration_lock = asyncio.Lock()
        self.playwright = None
        self.browser = None
        self.context = None
        self.pages = {}
        self.active = None
        self.counter = 0
        self.ref_base = {}
        self.ref_names = {}      # tab_id -> {ref: accessible name at snapshot time}
        self.ref_attribute = 'data-lp-ref-' + uuid.uuid4().hex[:12]
        self.dialogs = {}        # tab_id -> dialogs handled since the last result
        self.dialog_policy = {}  # tab_id -> (accept|dismiss, prompt text) while an action runs
        self.shot_seq = 0
        self.launched = None
        self.log_handle = None
        self.version = ''

    # ---- infrastructure --------------------------------------------------------------------------
    def _ensure_loop(self):
        with self.lock:
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
                threading.Thread(target=self.loop.run_forever, name='localpilot-browser', daemon=True).start()
        return self.loop

    def _run(self, coroutine, timeout=90, control=None, deadline=None, *, allow_disabled=False):
        if not self.enabled and not allow_disabled:
            coroutine.close()
            raise ToolError('浏览器控制未启用；在本机配置 browser.enabled=true 后重启连接。')
        control = control or CallControl()
        if deadline is not None:
            timeout = min(timeout, deadline - time.time())
        if timeout <= 0:
            coroutine.close()
            control.cancel(); control.stop_confirmed = True; control.settled.set()
            raise BridgeStopped('浏览器动作的任务时间已到，未开始执行。', 'BROWSER_TIMEOUT', control)
        async def tracked():
            try:
                with control.lock:
                    control.loop = asyncio.get_running_loop()
                    control.task = asyncio.current_task()
                if control.requested.is_set():
                    coroutine.close()
                    control.stop_confirmed = True
                    raise asyncio.CancelledError()
                return await coroutine
            finally:
                control.settled.set()
        future = asyncio.run_coroutine_threadsafe(tracked(), self._ensure_loop())
        try:
            result = future.result(timeout)
            if control.requested.is_set():
                raise BridgeStopped('已请求停止；浏览器可能已经执行了部分动作，请核对页面。', 'BROWSER_CANCELLED', control)
            return result
        except TimeoutError as exc:
            control.cancel(); control.settled.wait(5)
            raise BridgeStopped('浏览器动作已超时，后续操作已请求取消；已发送到页面的动作结果尚未确认。', 'BROWSER_TIMEOUT', control) from exc
        except FutureCancelledError as exc:
            control.settled.wait(5)
            # Cancelling a Playwright coroutine cannot undo an already sent CDP
            # command. Only cancellation before dispatch is a confirmed stop.
            raise BridgeStopped('浏览器动作已取消。' if control.stop_confirmed else
                                '已取消后续浏览器操作；已发送到页面的动作结果尚未确认，请核对页面。',
                                'BROWSER_CANCELLED', control) from exc
        except ToolError:
            raise
        except Exception as exc:
            # Playwright errors carry multi-line call logs; keep the first line so the model can react.
            message = str(exc).strip().split('\n')[0][:300]
            raise ToolError(f'浏览器操作失败：{message}') from exc

    def _probe(self):
        candidates = [self.cdp_url]
        if not self.explicit_cdp_url:
            # Never attach to an unrelated service/browser that happens to own
            # 9222. A managed Chrome must belong to this configured profile.
            if not (self.launched and self.launched.poll() is None) and self._profile_running(self.profile_dir) is not True:
                return None
            try:
                active = (self.profile_dir / 'DevToolsActivePort').read_text().splitlines()
                port = int(active[0])
                if 1 <= port <= 65535 and active[1].startswith('/devtools/browser/'):
                    candidates.insert(0, f'http://127.0.0.1:{port}')
            except (OSError, ValueError, IndexError):
                pass
        for endpoint in dict.fromkeys(candidates):
            try:
                local = urllib.parse.urlsplit(endpoint).hostname in ('127.0.0.1', 'localhost', '::1')
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if local else urllib.request.build_opener()
                with opener.open(endpoint.rstrip('/') + '/json/version', timeout=2) as response:
                    result = json.loads(response.read().decode('utf-8'))
                if not isinstance(result, dict) or not result.get('Browser') or not result.get('webSocketDebuggerUrl'):
                    continue
                self.cdp_url = endpoint
                return result
            except Exception:
                continue
        return None

    def _launch_port(self):
        # Chrome may silently bind only ::1 when IPv4 9222 is occupied, while
        # our client is pointed at 127.0.0.1. Port 0 gives Chrome a free port and
        # publishes it in its own profile's DevToolsActivePort for reconnects.
        try:
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', self.port))
            return self.port
        except OSError:
            return 0

    async def _launch(self):
        if not self.launch_if_missing:
            raise ToolError(f'{self.cdp_url} 没有可连接的 Chrome，且本机配置不允许自动启动。')
        if not self.chrome_path:
            raise ToolError('没有找到 Google Chrome；在本机配置 browser.chrome_path 指定路径。')
        if self.explicit_cdp_url:
            raise ToolError('配置的 browser.cdp_url 无法连接；不会另外启动一个与该地址无关的 Chrome。')
        if self._profile_running(self.profile_dir) is True:
            raise ToolError('LocalPilot 的 Chrome 已在运行，但调试端口不可达；请先关闭该窗口后重试，不会重复启动或覆盖进程句柄。')
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        (self.profile_dir / 'DevToolsActivePort').unlink(missing_ok=True)
        port = self._launch_port()
        args = [self.chrome_path, f'--remote-debugging-port={port}', f'--user-data-dir={self.profile_dir}', f'--window-size={self.window_size}',
                '--no-first-run', '--no-default-browser-check', '--disable-session-crashed-bubble', '--hide-crash-restore-bubble',
                '--disable-features=TranslateUI', '--restore-last-session', '--new-window', 'about:blank']
        if self.headless:
            args.insert(1, '--headless=new')
        if self.log_handle is not None:
            try:
                self.log_handle.close()
            except Exception:
                pass
        self.log_handle = open(self.state / 'chrome.log', 'ab')
        self.launched = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=self.log_handle, stdin=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if await asyncio.to_thread(self._probe):
                return
            if self.launched.poll() is not None:
                break
            await asyncio.sleep(0.3)
        raise ToolError('Chrome 已启动但调试端口没有就绪；查看 browser/chrome.log。若该端口被另一个 Chrome 占用，请改用 browser.cdp_port。')

    async def _ensure(self):
        async with self.lifecycle_lock:
            await self._connect()

    async def _connect(self):
        if self.browser is not None and self.browser.is_connected():
            return
        from playwright.async_api import async_playwright
        if self.playwright is None:
            self.playwright = await async_playwright().start()
        version = await asyncio.to_thread(self._probe)
        if version is None:
            await self._launch()
            version = await asyncio.to_thread(self._probe) or {}
        self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url, timeout=20000)
        self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
        self.context.set_default_timeout(self.nav_timeout)
        self.pages, self.active = {}, None
        for page in self.context.pages:
            await self._register(page)
        self.context.on('page', self._register)
        self.version = version.get('Browser', '')

    def _stable_tab_id(self, target_id):
        """Assign IDs to Chrome targets, not to the order pages happen to reconnect."""
        self.state.mkdir(parents=True, exist_ok=True)
        path = self.state / 'tab-ids.json'
        with open(self.state / 'tab-ids.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                registry = json.loads(path.read_text()) if path.exists() else {'next_id': 1, 'targets': {}}
                targets = registry['targets']
                if (not isinstance(targets, dict) or type(registry['next_id']) is not int or registry['next_id'] < 1 or
                    any(not isinstance(k, str) or not isinstance(v, str) or not re.fullmatch(r't\d+', v) for k, v in targets.items()) or
                    len(set(targets.values())) != len(targets)):
                    raise ValueError('invalid tab registry')
                if target_id in targets:
                    return targets[target_id]
                # Old versions had no registry and reused small IDs on restart.
                # Existing profiles begin a new range; stale old IDs fail closed.
                if not path.exists() and self.legacy_tab_ids:
                    registry['next_id'] = 1001
                number = max(registry['next_id'], max((int(v[1:]) + 1 for v in targets.values()), default=1))
                tab_id = f't{number}'
                targets[target_id] = tab_id; registry['next_id'] = number + 1
                temporary = path.with_name('.tabs-' + uuid.uuid4().hex)
                try:
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'w') as stream:
                        json.dump(registry, stream)
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
                return tab_id
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ToolError('浏览器标签标识记录不可读或损坏；未重新分配旧编号，以免操作错误页面。') from exc

    async def _register(self, page):
        async with self.registration_lock:
            if any(p is page for p in self.pages.values()) or page.is_closed():
                return
            session = await self.context.new_cdp_session(page)
            try:
                info = await session.send('Target.getTargetInfo')
                tab_id = self._stable_tab_id(info['targetInfo']['targetId'])
            finally:
                await session.detach()
            self.pages[tab_id] = page
            self.active = tab_id
            page.once('close', lambda: self._forget(tab_id))
            page.on('dialog', lambda dialog: self._on_dialog(tab_id, dialog))

    def _forget(self, tab_id):
        self.pages.pop(tab_id, None)
        for registry in (self.ref_base, self.ref_names, self.dialogs, self.dialog_policy):
            registry.pop(tab_id, None)
        if self.active == tab_id:
            self.active = next(reversed(self.pages), None)

    async def _on_dialog(self, tab_id, dialog):
        policy, text = self.dialog_policy.get(tab_id) or (None, None)
        kind = dialog.type
        entry = {'type': kind, 'message': (dialog.message or '')[:300]}
        try:
            if policy == 'accept' or (policy is None and kind == 'alert'):
                await dialog.accept(text if kind == 'prompt' and text is not None else None)
                entry['action'] = 'accepted'
            else:
                await dialog.dismiss()
                entry['action'] = 'dismissed'
        except Exception as exc:
            entry['action'] = f'error: {str(exc)[:80]}'
        if policy is None and kind in ('confirm', 'prompt', 'beforeunload'):
            entry['hint'] = "已按默认取消；如果用户确实要确认，重试同一操作并传 dialog='accept'。"
        history = self.dialogs.setdefault(tab_id, [])
        history.append(entry)
        del history[:-10]

    @contextmanager
    def _dialog_scope(self, tab_id, dialog, text=None):
        if dialog is not None:
            self.dialog_policy[tab_id] = (dialog, text)
        try:
            yield
        finally:
            self.dialog_policy.pop(tab_id, None)

    async def _page(self, tab_id=None):
        await self._ensure()
        if tab_id:
            page = self.pages.get(tab_id)
            if page is None or page.is_closed():
                raise ToolError(f'没有标签 {tab_id}；用 browser_tabs(action="list") 查看。')
            self.active = tab_id
            return page
        if self.active and self.active in self.pages and not self.pages[self.active].is_closed():
            return self.pages[self.active]
        live = [(k, p) for k, p in self.pages.items() if not p.is_closed()]
        if live:
            self.active = live[-1][0]
            return live[-1][1]
        page = await self.context.new_page()
        await self._register(page)
        return page

    def _tab_id(self, page):
        return next((k for k, p in self.pages.items() if p is page), None)

    async def _snapshot(self, page, mode='interactive', max_chars=None, selector=None):
        # Refs keep increasing for the life of a tab, so a ref from an older snapshot never silently hits another element.
        tab_id = self._tab_id(page)
        start = self.ref_base.get(tab_id, 0)
        opts = {'mode': mode, 'maxChars': max_chars or self.snapshot_chars, 'selector': selector, 'start': start,
                'refAttribute': self.ref_attribute}
        for attempt in range(2):
            try:
                data = await page.evaluate(SNAPSHOT_JS, opts)
                break
            except Exception as exc:
                # A click that navigates can destroy the document between settle and snapshot; wait for the new one once.
                if attempt or not any(word in str(exc) for word in ('destroyed', 'navigation', 'Navigation')):
                    raise
                try:
                    await page.wait_for_load_state('domcontentloaded', timeout=5000)
                except Exception:
                    pass
        if data.get('error'):
            raise ToolError(f'快照失败：{data["error"]}')
        self.ref_base[tab_id] = max(start, data['count'])
        names = self.ref_names.setdefault(tab_id, {})
        for node in data['nodes']:
            if node.get('ref'):
                names[node['ref']] = node.get('name', '')
        if len(names) > 5000:
            for key in list(names)[:-3000]:
                names.pop(key, None)
        return {'tab_id': tab_id, 'url': data['url'], 'title': data['title'], 'elements': data['listed'],
                'truncated': data['truncated'], 'viewport': data['viewport'], 'snapshot': render_snapshot(data)}

    async def _settle(self, page, timeout=4000):
        try:
            await page.wait_for_load_state('load', timeout=timeout)
        except Exception:
            pass
        try:
            await page.wait_for_load_state('networkidle', timeout=1500)
        except Exception:
            pass

    async def _title(self, page):
        try:
            return await page.title()
        except Exception:
            return ''

    async def _complete(self, page, result, snapshot, chars):
        """Common tail of every action: optional fresh snapshot, then the dialogs answered since the last result."""
        if snapshot:
            result.update(await self._snapshot(page, 'interactive', chars))
        dialogs = self.dialogs.pop(self._tab_id(page), None)
        if dialogs:
            result['dialogs'] = dialogs
        return result

    async def _goto(self, page, url, wait_until, timeout):
        """Navigate; when the page committed but never reached wait_until, report the timeout instead of failing the call."""
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        before = page.url
        try:
            return await page.goto(url, wait_until=wait_until, timeout=timeout), False
        except PlaywrightTimeout:
            if page.url == before and page.url != url:
                raise ToolError(f'{url} 在 {timeout / 1000:.0f} 秒内没有响应（导航没有提交）；检查地址或网络，或加大 timeout_ms。')
            return None, True

    def _locator(self, page, ref=None, selector=None, text=None):
        if ref:
            if not REF.match(ref):
                raise ToolError('ref 形如 e12，来自最近一次快照。')
            return page.locator(f'[{self.ref_attribute}="{ref}"]'), f'ref {ref}'
        if selector:
            return page.locator(selector).first, f'selector {selector}'
        if text:
            return page.get_by_text(text, exact=False).first, f'text {text!r}'
        raise ToolError('需要 ref（快照中的 e 编号）、selector 或 text 之一。')

    async def _resolve(self, page, ref, selector, text):
        locator, label = self._locator(page, ref, selector, text)
        count = await locator.count()
        if count == 0:
            if ref:
                raise ToolError(f'快照里的 {ref} 已不存在（页面可能变化）；先 browser_snapshot 再按新编号操作。')
            raise ToolError(f'找不到元素：{label}。')
        if ref:
            if count != 1:
                raise ToolError(f'{ref} 对应多个元素；页面可能复制了节点，请先重新读取快照。')
            # Refs are DOM nodes; recycled rows (virtual lists, live tables) keep the node but change its content.
            expected = self.ref_names.get(self._tab_id(page), {}).get(ref)
            if expected is None:
                raise ToolError(f'{ref} 不在本次连接已读取的快照中；请先 browser_snapshot。')
            if expected is not None:
                try:
                    current = await locator.evaluate(NAME_JS)
                except Exception:
                    current = None
                if current is not None and _norm(current) != _norm(expected):
                    raise ToolError(f'{ref} 在快照里是 {expected!r}，现在显示为 {current!r}，页面内容已变化；先 browser_snapshot 再按新编号操作。')
        return locator, label

    async def _remember(self, page, ref, locator):
        if not ref:
            return
        try:
            self.ref_names.setdefault(self._tab_id(page), {})[ref] = await locator.evaluate(NAME_JS)
        except Exception:
            pass

    @staticmethod
    def _check_dialog(dialog):
        if dialog is not None and dialog not in DIALOG_POLICIES:
            raise ToolError('dialog 取 accept 或 dismiss。')

    def _event(self, kind, details):
        return self.storage.event(f'browser_{kind}', details)

    # ---- public tools ------------------------------------------------------------------------------
    def navigate(self, url, tab_id=None, wait_until='load', snapshot=True, timeout_ms=None, *, _control=None, _deadline=None):
        if not isinstance(url, str) or not re.match(r'^(https?|file|about|chrome|data):', url):
            raise ToolError('url 必须是 http(s)://、file://、about: 或 chrome: 地址。')
        if wait_until not in ('load', 'domcontentloaded', 'networkidle', 'commit'):
            raise ToolError('wait_until 取 load、domcontentloaded、networkidle 或 commit。')
        timeout = int(timeout_ms or self.nav_timeout)
        if not 1000 <= timeout <= 120000:
            raise ToolError('timeout_ms 为 1000–120000。')
        async def run():
            page = await self._page(tab_id)
            response, timed_out = await self._goto(page, url, wait_until, timeout)
            await self._settle(page, 2000)
            result = {'tab_id': self._tab_id(page), 'url': page.url, 'title': await self._title(page), 'status': response.status if response else None}
            if timed_out:
                result['load_timed_out'] = True
                result['note'] = f'页面在 {timeout / 1000:.0f} 秒内没有触发 {wait_until}，已按当前状态返回快照；内容可能仍在加载。'
            return await self._complete(page, result, snapshot, 8000)
        result = self._run(run(), timeout / 1000 + 30, _control, _deadline)
        result['receipt_id'] = self._event('navigate', {'url': url, 'final_url': result['url'], 'tab_id': result['tab_id'], 'load_timed_out': bool(result.get('load_timed_out'))})
        return result

    def snapshot(self, tab_id=None, mode='interactive', max_chars=None, selector=None, *, _control=None, _deadline=None):
        if mode not in ('interactive', 'full', 'text'):
            raise ToolError('mode 取 interactive、full 或 text。')
        max_chars = int(max_chars or self.snapshot_chars)
        if not 500 <= max_chars <= 60000:
            raise ToolError('max_chars 为 500–60000。')
        async def run():
            page = await self._page(tab_id)
            if mode == 'text':
                script = 'sel => { const el = sel ? document.querySelector(sel) : (document.querySelector("main, article, [role=main]") || document.body); return el ? el.innerText : ""; }'
                text = await page.evaluate(script, selector)
                text = re.sub(r'\n{3,}', '\n\n', text or '').strip()
                result = {'tab_id': self._tab_id(page), 'url': page.url, 'title': await self._title(page), 'text': text[:max_chars],
                          'total_chars': len(text), 'truncated': len(text) > max_chars}
            else:
                result = await self._snapshot(page, mode, max_chars, selector)
            return await self._complete(page, result, False, 0)
        return self._run(run(), 60, _control, _deadline)

    def click(self, ref=None, selector=None, text=None, tab_id=None, button='left', double=False, snapshot=True, dialog=None, dialog_text=None, *, _control=None, _deadline=None):
        if button not in ('left', 'right', 'middle'):
            raise ToolError('button 取 left、right 或 middle。')
        self._check_dialog(dialog)
        async def run():
            page = await self._page(tab_id)
            locator, label = await self._resolve(page, ref, selector, text)
            with self._dialog_scope(self._tab_id(page), dialog, dialog_text):
                await locator.scroll_into_view_if_needed(timeout=5000)
                await locator.click(button=button, click_count=2 if double else 1, timeout=10000)
                await self._settle(page)
            await self._remember(page, ref, locator)
            result = {'tab_id': self._tab_id(page), 'clicked': label, 'url': page.url, 'title': await self._title(page)}
            return await self._complete(page, result, snapshot, 8000)
        result = self._run(run(), 60, _control, _deadline)
        result['receipt_id'] = self._event('click', {'target': result['clicked'], 'url': result['url'], 'dialogs': len(result.get('dialogs', []))})
        return result

    def type(self, text, ref=None, selector=None, tab_id=None, submit=False, clear=True, snapshot=True, dialog=None, *, _control=None, _deadline=None):
        if not isinstance(text, str):
            raise ToolError('text 必须是字符串。')
        self._check_dialog(dialog)
        async def run():
            page = await self._page(tab_id)
            locator, label = await self._resolve(page, ref, selector, None)
            with self._dialog_scope(self._tab_id(page), dialog):
                await locator.scroll_into_view_if_needed(timeout=5000)
                if clear:
                    await locator.fill(text, timeout=10000)
                else:
                    await locator.click(timeout=10000)
                    await locator.press_sequentially(text, timeout=10000)
                if submit:
                    await locator.press('Enter')
                    await self._settle(page)
            await self._remember(page, ref, locator)
            result = {'tab_id': self._tab_id(page), 'typed_into': label, 'chars': len(text), 'submitted': bool(submit), 'url': page.url}
            return await self._complete(page, result, snapshot, 6000)
        result = self._run(run(), 60, _control, _deadline)
        result['receipt_id'] = self._event('type', {'target': result['typed_into'], 'chars': len(text), 'submit': bool(submit)})
        return result

    def act(self, action, ref=None, selector=None, text=None, value=None, key=None, tab_id=None, snapshot=True, timeout_ms=10000, dialog=None, *, _control=None, _deadline=None):
        if action not in ACTIONS:
            raise ToolError(f'action 取 {", ".join(ACTIONS)}。')
        self._check_dialog(dialog)
        async def run():
            page = await self._page(tab_id)
            detail = {}
            locator = None
            with self._dialog_scope(self._tab_id(page), dialog):
                if action in ('hover', 'check', 'uncheck', 'focus', 'clear', 'select_option') or (action == 'press_key' and (ref or selector)):
                    locator, label = await self._resolve(page, ref, selector, text)
                    detail['target'] = label
                    if action == 'hover': await locator.hover(timeout=timeout_ms)
                    elif action == 'check': await locator.check(timeout=timeout_ms)
                    elif action == 'uncheck': await locator.uncheck(timeout=timeout_ms)
                    elif action == 'focus': await locator.focus(timeout=timeout_ms)
                    elif action == 'clear': await locator.fill('', timeout=timeout_ms)
                    elif action == 'select_option':
                        values = value if isinstance(value, list) else [value]
                        if not values or values == [None]:
                            raise ToolError('select_option 需要 value（选项值、标签或列表）。')
                        try:
                            detail['selected'] = await locator.select_option([str(v) for v in values], timeout=timeout_ms)
                        except Exception:
                            detail['selected'] = await locator.select_option(label=[str(v) for v in values], timeout=timeout_ms)
                    elif action == 'press_key':
                        if not key: raise ToolError('press_key 需要 key，例如 Enter、Tab、Escape、ArrowDown、Control+a。')
                        await locator.press(key, timeout=timeout_ms)
                elif action == 'press_key':
                    if not key: raise ToolError('press_key 需要 key。')
                    await page.keyboard.press(key)
                elif action == 'scroll':
                    if ref or selector:
                        locator, label = await self._resolve(page, ref, selector, None)
                        await locator.scroll_into_view_if_needed(timeout=timeout_ms); detail['target'] = label
                    else:
                        delta = int(value) if value not in (None, '') else 800
                        await page.mouse.wheel(0, delta); detail['scrolled_by'] = delta
                elif action == 'back': await page.go_back(wait_until='commit', timeout=self.nav_timeout)
                elif action == 'forward': await page.go_forward(wait_until='commit', timeout=self.nav_timeout)
                elif action == 'reload': await page.reload(wait_until='domcontentloaded', timeout=self.nav_timeout)
                elif action == 'wait_for':
                    if text: await page.get_by_text(text, exact=False).first.wait_for(timeout=timeout_ms); detail['waited_for'] = f'text {text!r}'
                    elif selector: await page.locator(selector).first.wait_for(timeout=timeout_ms); detail['waited_for'] = f'selector {selector}'
                    else:
                        ms = int(value) if value not in (None, '') else 1000
                        await page.wait_for_timeout(min(ms, 30000)); detail['waited_ms'] = min(ms, 30000)
                await self._settle(page, 3000)
            if locator is not None:
                await self._remember(page, ref, locator)
            result = {'tab_id': self._tab_id(page), 'action': action, **detail, 'url': page.url, 'title': await self._title(page)}
            return await self._complete(page, result, snapshot, 6000)
        result = self._run(run(), 60, _control, _deadline)
        result['receipt_id'] = self._event('act', {'action': action, 'target': result.get('target'), 'url': result['url']})
        return result

    def evaluate(self, expression, tab_id=None, arg=None, max_chars=20000, *, _control=None, _deadline=None):
        if not isinstance(expression, str) or not expression.strip():
            raise ToolError('expression 不能为空。')
        async def run():
            page = await self._page(tab_id)
            value = await page.evaluate(expression, arg)
            text = json.dumps(value, ensure_ascii=False, default=str)
            result = {'tab_id': self._tab_id(page), 'url': page.url, 'result': value if len(text) <= max_chars else None,
                      'result_json': text[:max_chars], 'truncated': len(text) > max_chars}
            return await self._complete(page, result, False, 0)
        result = self._run(run(), 60, _control, _deadline)
        result['receipt_id'] = self._event('evaluate', {'expression': expression[:300], 'url': result['url'], 'truncated': result['truncated']})
        return result

    def screenshot(self, tab_id=None, full_page=False, ref=None, selector=None, max_side=None, max_tiles=3, *, _control=None, _deadline=None):
        from PIL import Image
        max_side = self.max_side if max_side is None else max_side
        if type(max_side) is not int or not 256 <= max_side <= 4096:
            raise ToolError('max_side 为 256–4096。')
        if type(max_tiles) is not int or not 1 <= max_tiles <= 6:
            raise ToolError('max_tiles 为 1–6。')
        async def run():
            page = await self._page(tab_id)
            viewport = await page.evaluate('({width: innerWidth, height: innerHeight, dpr: devicePixelRatio, pageWidth: Math.max(document.documentElement.scrollWidth,innerWidth), pageHeight: Math.max(document.documentElement.scrollHeight,innerHeight)})')
            if ref or selector:
                locator, label = await self._resolve(page, ref, selector, None)
                data = await locator.screenshot(type='png', timeout=15000)
            else:
                label = 'page'
                if full_page and viewport['pageWidth'] * viewport['pageHeight'] * viewport['dpr']**2 > MAX_SOURCE_PIXELS:
                    raise ToolError('整页截图超过 4000 万像素；请滚动并截取视口，或指定元素。')
                data = await page.screenshot(type='png', full_page=bool(full_page), timeout=30000)
            return page, label, data, viewport
        page, label, data, viewport = self._run(run(), 90, _control, _deadline)
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width * height > MAX_SOURCE_PIXELS:
                raise ToolError('截图超过 4000 万像素；请改为视口截图或指定元素。')
            picture = image.convert('RGB') if image.mode not in ('RGB', 'RGBA') else image.copy()
        # A tall full-page capture is cut into viewport-high tiles; shrinking it whole would leave nothing legible.
        tiles, total_tiles = [picture], 1
        if full_page and label == 'page' and height > width * 1.2:
            scale = width / max(int(viewport.get('width') or 1), 1)
            tile_height = max(int(int(viewport.get('height') or 800) * scale), 400)
            total_tiles = math.ceil(height / tile_height)
            tiles = [picture.crop((0, index * tile_height, width, min((index + 1) * tile_height, height))) for index in range(min(total_tiles, max_tiles))]
        images, sizes, metadata = [], [], []
        for tile in tiles:
            tile.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            encoded, mime, dims = _encode_image(tile)
            images.append({'data': base64.b64encode(encoded).decode('ascii'), 'mime_type': mime})
            sizes.append([*dims, len(encoded)])
            metadata.append({'mime_type': mime, 'width': dims[0], 'height': dims[1], 'bytes': len(encoded),
                             'sha256': hashlib.sha256(encoded).hexdigest()})
        shots = self.state / 'screenshots'
        shots.mkdir(parents=True, exist_ok=True)
        self.shot_seq += 1
        path = shots / f'{time.strftime("%Y%m%d-%H%M%S")}-{self._tab_id(page)}-{uuid.uuid4().hex[:12]}.png'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        result = {'tab_id': self._tab_id(page), 'url': page.url, 'target': label, 'path': str(path), 'original_width': width, 'original_height': height,
                  'width': sizes[0][0], 'height': sizes[0][1], 'bytes': sum(size[2] for size in sizes), 'tiles': len(images), 'total_tiles': total_tiles,
                  'tile_sizes': sizes, 'image_metadata': metadata, 'images': images,
                  'mime_type': metadata[0]['mime_type'], 'image_sha256': metadata[0]['sha256'],
                  'image_bytes': metadata[0]['bytes']}
        if total_tiles > len(images):
            result['note'] = f'整页共 {total_tiles} 屏，只返回前 {len(images)} 屏；需要更多时滚动后再截图，或提高 max_tiles。'
        result['receipt_id'] = self._event('screenshot', {'path': str(path), 'url': page.url, 'target': label, 'tiles': len(images)})
        return result

    def tabs(self, action='list', tab_id=None, url=None, force=False, profile=None, *, _control=None, _deadline=None):
        if action not in TAB_ACTIONS:
            raise ToolError(f'action 取 {", ".join(TAB_ACTIONS)}。')
        if action == 'status':
            return self._run(asyncio.to_thread(self.status), 15, _control, _deadline, allow_disabled=True)
        if action == 'clone_logins':
            return self.clone_logins(force, profile, _control=_control, _deadline=_deadline)
        if action == 'stop':
            return self.stop(_control=_control, _deadline=_deadline)
        async def run():
            await self._ensure()
            note = None
            if action == 'open':
                page = await self.context.new_page()
                await self._register(page)
                if url:
                    _, timed_out = await self._goto(page, url, 'load', self.nav_timeout)
                    await self._settle(page, 2000)
                    if timed_out:
                        note = '页面在超时前没有加载完成，标签已打开。'
            elif action == 'close':
                page = self.pages.get(tab_id or self.active)
                if page is None: raise ToolError('没有这个标签。')
                await page.close()
            elif action == 'select':
                page = await self._page(tab_id)
                await page.bring_to_front()
            rows = []
            for key, page in list(self.pages.items()):
                if page.is_closed(): continue
                rows.append({'tab_id': key, 'url': page.url, 'title': (await self._title(page))[:120], 'active': key == self.active})
            result = {'action': action, 'active': self.active, 'tabs': rows}
            if note:
                result['note'] = note
            return result
        return self._run(run(), self.nav_timeout / 1000 + 30, _control, _deadline)

    def status(self):
        probe = self._probe()
        profiles = self._profiles()
        return {'enabled': self.enabled, 'cdp_url': self.cdp_url, 'connected': bool(self.browser and self.browser.is_connected()),
                'chrome_reachable': probe is not None, 'chrome': (probe or {}).get('Browser'), 'profile_dir': str(self.profile_dir),
                'profile_exists': self.profile_dir.exists(), 'launched_by_localpilot': bool(self.launched and self.launched.poll() is None),
                'headless': self.headless, 'window_size': self.window_size, 'chrome_path': self.chrome_path, 'user_chrome_running': self._user_chrome_running(),
                'user_chrome_dir': str(self.user_chrome), 'user_profiles': [{k: v for k, v in row.items() if k != 'mtime'} for row in profiles[:8]],
                'tabs': len([p for p in self.pages.values() if not p.is_closed()]),
                'login_note': ('LocalPilot reuses its persistent Chrome profile and restores session cookies when restarting it; a new profile initially has no logins. Log in once in the LocalPilot Chrome window, '
                               'or run browser_tabs(action="clone_logins") with the user\'s Chrome closed to copy cookies from their most recently used profile '
                               '(profile= picks another). Copied logins that were not on disk, or expired/revoked SSO sessions, may need a fresh login.')}

    def _user_chrome_running(self):
        """True when a Chrome that uses the user's own data directory is running (LocalPilot's own Chrome does not count)."""
        return self._profile_running(self.user_chrome)

    def _profile_running(self, profile_dir):
        try:
            result = subprocess.run(['ps', '-axo', 'command='], capture_output=True, text=True, timeout=5, check=True)
            out = result.stdout
        except Exception:
            return None
        default_dir = str(USER_CHROME)
        for line in out.splitlines():
            if 'Helper' in line or 'crashpad' in line or not re.search(r'/(Google Chrome|Google Chrome Canary|Chromium)(\s|$)', line):
                continue
            match = re.search(r'--user-data-dir=(.+?)(?= --|$)', line)
            data_dir = match.group(1).strip() if match else default_dir
            if Path(data_dir.strip('\"\'')).expanduser().resolve() == profile_dir.resolve():
                return True
        return False

    def _target_busy(self):
        # The Chrome process deliberately survives an MCP restart. A missing
        # Python connection handle says nothing about the profile's ownership.
        if (self.browser is not None and self.browser.is_connected()) or self._probe() is not None:
            return True
        running = self._profile_running(self.profile_dir)
        if running is not False:
            return True  # Unable to inspect processes is not proof of inactivity.
        lock = self.profile_dir / 'SingletonLock'
        if lock.is_symlink():
            try:
                pid = int(os.readlink(lock).rsplit('-', 1)[-1])
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                pass
            except (OSError, ValueError):
                return True
        elif lock.exists():
            return True
        return False

    @staticmethod
    def _cookie_file(profile):
        for name in ('Network/Cookies', 'Cookies'):
            if (profile / name).is_file():
                return profile / name
        return None

    def _profiles(self):
        """The user's Chrome profiles: directory, display name, which one Chrome used last, when its cookies changed."""
        info, last_used = {}, None
        try:
            data = json.loads((self.user_chrome / 'Local State').read_text('utf-8'))
            info = data.get('profile', {}).get('info_cache', {}) or {}
            last_used = data.get('profile', {}).get('last_used')
        except Exception:
            pass
        directories = set(info)
        if self.user_chrome.is_dir():
            directories |= {p.name for p in self.user_chrome.iterdir() if p.is_dir() and (p.name == 'Default' or p.name.startswith('Profile '))}
        rows = []
        for directory in directories:
            cookies = self._cookie_file(self.user_chrome / directory)
            mtime = cookies.stat().st_mtime if cookies else 0
            rows.append({'dir': directory, 'name': str((info.get(directory) or {}).get('name', '')), 'last_used': directory == last_used,
                         'cookies_modified': time.strftime('%Y-%m-%d', time.localtime(mtime)) if mtime else None, 'mtime': mtime})
        rows.sort(key=lambda row: (0 if row['last_used'] else 1, -row['mtime'], row['dir']))
        return rows

    def _pick_profile(self, rows, profile):
        if profile:
            wanted = str(profile).strip()
            for row in rows:
                if row['dir'] == wanted or (row['name'] and row['name'].lower() == wanted.lower()):
                    return row
            options = ', '.join(f"{row['dir']}{' (' + row['name'] + ')' if row['name'] else ''}" for row in rows) or '无'
            raise ToolError(f'没有名为 {wanted!r} 的 Chrome 配置；可选：{options}。')
        if rows:
            return rows[0]
        return {'dir': 'Default', 'name': '', 'last_used': False, 'cookies_modified': None, 'mtime': 0}

    def clone_logins(self, force=False, profile=None, *, _control=None, _deadline=None):
        control = _control or CallControl()
        async def run():
            async with self.lifecycle_lock:
                # Hold the lifecycle lock until the worker actually stops, even
                # when its awaiting coroutine is cancelled. No launch during copy.
                worker = asyncio.create_task(asyncio.to_thread(self._clone_logins, force, profile, control, _deadline))
                try:
                    return await asyncio.shield(worker)
                except asyncio.CancelledError:
                    control.requested.set()
                    try:
                        await worker
                    except (ToolError, OSError):
                        pass
                    raise
        return self._run(run(), 90, control, _deadline)

    def _clone_logins(self, force, profile, control, deadline):
        """Copy cookies and login state from the user's Chrome profile (last used unless profile= says otherwise) into the LocalPilot profile."""
        def checkpoint():
            if control.requested.is_set() or (deadline is not None and time.time() >= deadline):
                raise ToolError('登录态复制已停止，已复制的部分文件予以保留。')
        checkpoint()
        if self._target_busy():
            raise ToolError('LocalPilot 的目标 Chrome 配置仍在使用中，或无法确认已关闭；请关闭该 Chrome 后再复制登录态。force 不能覆盖正在使用的目标。')
        rows = self._profiles()
        chosen = self._pick_profile(rows, profile)
        source = self.user_chrome / chosen['dir']
        if not source.is_dir():
            raise ToolError(f'没有找到 Chrome 配置目录 {source}。')
        if self.browser is not None and self.browser.is_connected():
            raise ToolError('先 browser_tabs(action="stop") 关闭 LocalPilot 的 Chrome，再复制登录态。')
        if self._user_chrome_running() and not force:
            raise ToolError('用户的 Chrome 正在运行，复制中的数据库可能不完整。请先退出 Chrome 再调用，或在用户确认后传 force=true。')
        target = self.profile_dir / 'Default'
        if source.resolve() == target.resolve() or source.resolve() in target.resolve().parents or target.resolve() in source.resolve().parents:
            raise ToolError('源 Chrome 配置与目标配置重叠，拒绝复制。')
        checkpoint()
        target.mkdir(parents=True, exist_ok=True)
        copied, errors = [], []
        for name in PROFILE_FILES:
            checkpoint()
            src = source / name
            if not src.is_file():
                continue
            dst = target / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                try:
                    _copy_sqlite(src, dst, checkpoint)
                except sqlite3.Error:
                    shutil.copy2(src, dst)
                copied.append(name)
            except OSError as exc:
                errors.append({'file': name, 'error': str(exc)[:200]})
        for folder in PROFILE_DIRS:
            checkpoint()
            src = source / folder
            if src.is_dir():
                try:
                    def copy_file(src, dst):
                        checkpoint()
                        return shutil.copy2(src, dst)
                    shutil.copytree(src, target / folder, dirs_exist_ok=True, copy_function=copy_file); copied.append(folder)
                except OSError as exc:
                    errors.append({'file': folder, 'error': str(exc)[:200]})
        # _launch uses --restore-last-session for both copied and interactive
        # logins. Do not rewrite Chrome's protected startup preferences.
        self._event('clone_logins', {'profile': chosen['dir'], 'copied': copied, 'errors': len(errors), 'forced': bool(force)})
        return {'profile_dir': str(self.profile_dir), 'source_profile': {'dir': chosen['dir'], 'name': chosen['name'], 'last_used': chosen['last_used']},
                'copied': copied, 'errors': errors, 'user_profiles': [{k: v for k, v in row.items() if k != 'mtime'} for row in rows[:8]],
                'note': ('Cookies decrypt through the same macOS Keychain entry, so persistent logins work in the LocalPilot Chrome. '
                         'Session-only logins survive only if they were on disk at copy time; if a site still asks, log in once in the LocalPilot window.')}

    def stop(self, *, _control=None, _deadline=None):
        async def run():
            async with self.lifecycle_lock:
                stopped = False
                # Chrome survives an MCP restart; its old Popen handle does not.
                managed = (not self.explicit_cdp_url and
                           self.profile_dir.resolve() not in (self.user_chrome.resolve(), USER_CHROME.resolve()) and
                           await asyncio.to_thread(self._profile_running, self.profile_dir) is True)
                if managed and not (self.launched and self.launched.poll() is None):
                    probe = await asyncio.to_thread(self._probe)
                    if probe is None:
                        raise ToolError('LocalPilot 的 Chrome 仍在运行，但无法连接以关闭；未报告停止成功。请关闭该窗口后重试。')
                    if self.playwright is None:
                        from playwright.async_api import async_playwright
                        self.playwright = await async_playwright().start()
                    if self.browser is None or not self.browser.is_connected():
                        self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url, timeout=10000)
                    session = await self.browser.new_browser_cdp_session()
                    try:
                        await session.send('Browser.close')
                    except Exception:
                        pass  # Closing Chrome can disconnect CDP before its reply.
                    until = time.monotonic() + 10
                    while time.monotonic() < until:
                        if await asyncio.to_thread(self._profile_running, self.profile_dir) is False:
                            stopped = True
                            break
                        await asyncio.sleep(.1)
                    if not stopped:
                        raise ToolError('已请求关闭 LocalPilot Chrome，但进程退出尚未确认。')
                if self.browser is not None:
                    try:
                        await self.browser.close()
                    except Exception:
                        pass
                self.browser, self.context, self.pages, self.active = None, None, {}, None
                self.ref_base, self.ref_names, self.dialogs, self.dialog_policy = {}, {}, {}, {}
                if self.launched and self.launched.poll() is None:
                    self.launched.terminate()
                    try:
                        await asyncio.to_thread(self.launched.wait, 10)
                    except subprocess.TimeoutExpired:
                        self.launched.kill()
                        await asyncio.to_thread(self.launched.wait, 5)
                    stopped = True
                self.launched = None
                if self.log_handle is not None:
                    self.log_handle.close(); self.log_handle = None
                return {'stopped_localpilot_chrome': stopped, 'note': 'A Chrome that LocalPilot did not launch is left running.'}
        return self._run(run(), 30, _control, _deadline, allow_disabled=True)

    def shutdown(self):
        # Leave the Chrome window alone; only drop the CDP connection so the user keeps their tabs.
        async def run():
            if self.browser is not None:
                try:
                    await self.browser.close()
                except Exception:
                    pass
        if self.loop is not None and self.browser is not None:
            try:
                asyncio.run_coroutine_threadsafe(run(), self.loop).result(10)
            except Exception:
                pass
