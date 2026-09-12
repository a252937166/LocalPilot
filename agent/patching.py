"""Multi-hunk patch parsing and application in the Codex apply_patch grammar (plus unified diff)."""
from __future__ import annotations
import re
from mcp.server.mcpserver.exceptions import ToolError

MAX_PATCH_CHARS = 512 * 1024
UNIFIED_HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$')


def parse_patch(text):
    """Return an ordered list of file operations. Raises ToolError on malformed input."""
    if not isinstance(text, str) or not text.strip():
        raise ToolError('patch 不能为空。')
    if len(text) > MAX_PATCH_CHARS:
        raise ToolError(f'patch 超过 {MAX_PATCH_CHARS} 字符上限。')
    lines = text.replace('\r\n', '\n').split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    if any(line.startswith('*** ') for line in lines[:3]) or (lines and lines[0].strip() == '*** Begin Patch'):
        return _parse_codex(lines)
    if any(line.startswith('--- ') for line in lines[:5]) and any(line.startswith('+++ ') for line in lines[:6]):
        return _parse_unified(lines)
    raise ToolError('无法识别的补丁格式。请使用 *** Begin Patch / *** Update File: 路径 / @@ / +- 行 / *** End Patch，或标准 unified diff。')


def _strip_prefix(path):
    path = path.strip()
    for prefix in ('a/', 'b/'):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def _parse_codex(lines):
    index, ops = 0, []
    if lines and lines[0].strip() == '*** Begin Patch':
        index = 1
    current = None
    while index < len(lines):
        line = lines[index]
        if line.strip() == '*** End Patch':
            index += 1
            break
        if line.startswith('*** Add File: '):
            current = {'kind': 'add', 'path': line[len('*** Add File: '):].strip(), 'lines': []}
            ops.append(current); index += 1
            while index < len(lines) and not lines[index].startswith('*** '):
                body = lines[index]
                if not body.startswith('+'):
                    raise ToolError(f'Add File 的每一行必须以 + 开头（第 {index + 1} 行）。')
                current['lines'].append(body[1:]); index += 1
            continue
        if line.startswith('*** Delete File: '):
            ops.append({'kind': 'delete', 'path': line[len('*** Delete File: '):].strip()}); index += 1
            continue
        if line.startswith('*** Update File: '):
            current = {'kind': 'update', 'path': line[len('*** Update File: '):].strip(), 'move_to': None, 'hunks': []}
            ops.append(current); index += 1
            if index < len(lines) and lines[index].startswith('*** Move to: '):
                current['move_to'] = lines[index][len('*** Move to: '):].strip(); index += 1
            hunk = None
            while index < len(lines) and not (lines[index].startswith('*** ') and lines[index].strip() != '*** End of File'):
                body = lines[index]
                if body.startswith('@@'):
                    header = body[2:].strip() or None
                    hunk = {'header': header, 'lines': [], 'eof': False}
                    current['hunks'].append(hunk)
                elif body.strip() == '*** End of File':
                    if hunk is None:
                        raise ToolError(f'*** End of File 前需要有 @@ 块（第 {index + 1} 行）。')
                    hunk['eof'] = True
                else:
                    if hunk is None:
                        hunk = {'header': None, 'lines': [], 'eof': False}
                        current['hunks'].append(hunk)
                    mark = body[:1]
                    if mark not in (' ', '-', '+') and body != '':
                        raise ToolError(f'Update File 的行必须以空格、- 或 + 开头（第 {index + 1} 行：{body[:40]!r}）。')
                    hunk['lines'].append((mark or ' ', body[1:] if body else ''))
                index += 1
            if not current['hunks'] and not current['move_to']:
                raise ToolError(f'Update File {current["path"]} 没有任何修改块。')
            continue
        if not line.strip():
            index += 1
            continue
        raise ToolError(f'无法解析的补丁行（第 {index + 1} 行）：{line[:60]!r}')
    if not ops:
        raise ToolError('补丁没有包含任何文件操作。')
    return ops


def _parse_unified(lines):
    ops, index = [], 0
    while index < len(lines):
        if not lines[index].startswith('--- '):
            index += 1
            continue
        old_path = _strip_prefix(lines[index][4:].split('\t')[0])
        index += 1
        if index >= len(lines) or not lines[index].startswith('+++ '):
            raise ToolError('unified diff 缺少 +++ 行。')
        new_path = _strip_prefix(lines[index][4:].split('\t')[0]); index += 1
        kind = 'add' if old_path == '/dev/null' else 'delete' if new_path == '/dev/null' else 'update'
        op = {'kind': kind, 'path': new_path if kind == 'add' else old_path,
              'move_to': new_path if kind == 'update' and new_path != old_path else None, 'hunks': []}
        ops.append(op)
        while index < len(lines) and lines[index].startswith('@@'):
            match = UNIFIED_HUNK.fullmatch(lines[index])
            if not match:
                raise ToolError('unified diff 的 @@ 行号或行数不合法。')
            hunk = {'format': 'unified', 'old_start': int(match[1]), 'old_count': int(match[2] or 1),
                    'new_start': int(match[3]), 'new_count': int(match[4] or 1), 'lines': [],
                    'old_no_newline': False, 'new_no_newline': False}
            index += 1; old_count = new_count = 0
            while old_count < hunk['old_count'] or new_count < hunk['new_count']:
                if index >= len(lines) or lines[index][:1] not in (' ', '-', '+'):
                    raise ToolError('unified diff 的块内行数与 @@ 声明不一致。')
                body = lines[index]; mark = body[0]
                old_count += mark in (' ', '-'); new_count += mark in (' ', '+')
                if old_count > hunk['old_count'] or new_count > hunk['new_count']:
                    raise ToolError('unified diff 的块内行数超过 @@ 声明。')
                hunk['lines'].append((mark, body[1:])); index += 1
                if index < len(lines) and lines[index] == '\\ No newline at end of file':
                    if mark in (' ', '-'): hunk['old_no_newline'] = True
                    if mark in (' ', '+'): hunk['new_no_newline'] = True
                    index += 1
            op['hunks'].append(hunk)
            if index < len(lines) and lines[index][:1] in (' ', '-', '+', '\\') and not lines[index].startswith('--- '):
                raise ToolError('unified diff 包含未计入 @@ 的内容。')
        if not op['hunks']:
            raise ToolError(f'{old_path} 的 diff 没有 @@ 块。')
    if not ops:
        raise ToolError('unified diff 没有文件操作。')
    return ops


def _find(haystack, needle, start, normalize):
    if not needle:
        return start
    target = [normalize(x) for x in needle]
    limit = len(haystack) - len(needle)
    for position in range(start, limit + 1):
        if all(normalize(haystack[position + k]) == target[k] for k in range(len(needle))):
            return position
    return -1


def apply_hunks(original, hunks, path='file'):
    """Apply hunks in order to the file text; returns (new_text, stats). Context must match, with whitespace fallbacks."""
    newline = '\r\n' if '\r\n' in original else '\n'
    text = original.replace('\r\n', '\n')
    had_trailing = text.endswith('\n')
    lines = text.split('\n') if text else []
    if had_trailing:
        lines.pop()
    cursor, added, removed, delta, old_end = 0, 0, 0, 0, 0
    for number, hunk in enumerate(hunks, 1):
        old = [content for mark, content in hunk['lines'] if mark in (' ', '-')]
        new = [content for mark, content in hunk['lines'] if mark in (' ', '+')]
        if hunk.get('format') == 'unified':
            base = hunk['old_start'] - (1 if hunk['old_count'] else 0)
            position = base + delta
            new_base = hunk['new_start'] - (1 if hunk['new_count'] else 0)
            if (base < old_end or position < 0 or position > len(lines) or new_base != position
                    or lines[position:position + len(old)] != old):
                raise ToolError(f'{path} 的第 {number} 个 unified 修改块与指定行号处的内容不匹配；请重新读取。')
            touches_end = position + len(old) == len(lines)
            if old and touches_end and had_trailing == hunk['old_no_newline']:
                raise ToolError(f'{path} 的文件末尾换行与补丁不匹配。')
            if hunk['old_no_newline'] and not touches_end:
                raise ToolError(f'{path} 的无换行标记不在文件末尾。')
            lines[position:position + len(old)] = new
            if hunk['new_no_newline'] and position + len(new) != len(lines):
                raise ToolError(f'{path} 的新增无换行标记不在文件末尾。')
            if touches_end:
                had_trailing = bool(lines) and not hunk['new_no_newline']
            old_end = base + len(old); delta += len(new) - len(old)
            removed += sum(mark == '-' for mark, _ in hunk['lines'])
            added += sum(mark == '+' for mark, _ in hunk['lines'])
            cursor = position + len(new)
            continue
        start = cursor
        if hunk.get('header'):
            anchor = _find(lines, [hunk['header']], cursor, str.strip)
            if anchor < 0:
                anchor = _find(lines, [hunk['header']], 0, str.strip)
            if anchor >= 0:
                start = anchor
        position = -1
        for normalize in (lambda s: s, str.rstrip, str.strip):
            position = _find(lines, old, start, normalize)
            if position < 0 and start:
                position = _find(lines, old, 0, normalize)
            if position >= 0:
                break
        if position < 0:
            raise ToolError(f'{path} 的第 {number} 个修改块与当前文件内容不匹配；请重新 read_file 后按实际内容提供上下文。')
        if hunk.get('eof') and position + len(old) != len(lines):
            eof_position = len(lines) - len(old)
            if eof_position >= 0 and all(lines[eof_position + k].rstrip() == old[k].rstrip() for k in range(len(old))):
                position = eof_position
            else:
                raise ToolError(f'{path} 的第 {number} 个修改块声明位于文件末尾，但末尾内容不匹配。')
        lines[position:position + len(old)] = new
        removed += sum(1 for mark, _ in hunk['lines'] if mark == '-')
        added += sum(1 for mark, _ in hunk['lines'] if mark == '+')
        cursor = position + len(new)
    result = newline.join(lines)
    if had_trailing or (not original and lines and not any(h.get('format') == 'unified' for h in hunks)):
        result += newline
    return result, {'hunks_applied': len(hunks), 'lines_added': added, 'lines_removed': removed}
