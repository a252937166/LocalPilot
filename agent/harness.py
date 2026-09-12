"""A model-free task controller inspired by Codex's plan/turn/goal boundaries.

The Chat host owns inference. This module owns task state, step dispatch,
receipts, completion checks and bounded continuation requests, never model calls.
"""
from __future__ import annotations
import hashlib
import inspect
import json
import threading
import time
import uuid
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from mcp.server.mcpserver.exceptions import ToolError
from presentation import public_receipt


class PlanItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=64)
    step: str = Field(min_length=1, max_length=500)
    status: Literal['pending', 'in_progress', 'completed'] = 'pending'


class Check(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['file_contains', 'file_equals', 'file_sha256', 'job_succeeded', 'image_saved']
    path: str | None = None
    value: str | None = Field(default=None, max_length=8000)
    action_id: str | None = Field(default=None, max_length=80)
    min_duration_seconds: int = Field(default=0, ge=0, le=86400)


class CompletionAssessment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope_summary: str = Field(min_length=20, max_length=4000)
    evidence_action_ids: list[str] = Field(min_length=1, max_length=100)
    remaining_work: list[str] = Field(default_factory=list, max_length=30)


Operation = Literal['get_project_context', 'find_skills', 'read_local_skill', 'list_directory', 'read_file', 'read_image', 'search_files', 'write_file',
                    'replace_text', 'apply_patch', 'edit_image', 'download_image', 'run_shell', 'job_status', 'cancel_job', 'list_mcp_tools', 'call_mcp_tool',
                    'browser_navigate', 'browser_snapshot', 'browser_click', 'browser_type', 'browser_act', 'browser_screenshot', 'browser_evaluate', 'browser_tabs']
MUTATIONS = {'write_file', 'replace_text', 'apply_patch', 'edit_image', 'save_chat_image', 'download_image', 'run_shell', 'call_mcp_tool',
             'browser_navigate', 'browser_click', 'browser_type', 'browser_act', 'browser_evaluate', 'browser_tabs'}
BRIDGE_OPERATIONS = {'list_mcp_tools', 'call_mcp_tool', 'browser_navigate', 'browser_snapshot', 'browser_click', 'browser_type', 'browser_act', 'browser_screenshot', 'browser_evaluate', 'browser_tabs'}
CONTROLLED_OPERATIONS = (BRIDGE_OPERATIONS - {'list_mcp_tools'}) | {'edit_image', 'download_image', 'save_chat_image'}
TERMINAL_JOBS = {'completed', 'failed', 'cancelled', 'timed_out', 'interrupted'}
# Scalar inputs copied verbatim into the receipt; file bodies are summarised instead.
INPUT_KEYS = ('dialog', 'profile', 'max_tiles', 'timeout_ms', 'wait_until', 'command', 'cwd', 'path', 'job_id', 'timeout_seconds', 'wait_seconds', 'pattern', 'query',
              'limit', 'offset', 'max_chars', 'expected_sha256', 'create_parents', 'max_side', 'crop', 'frame_index',
              'output_path', 'output_expected_sha256', 'resize', 'rotate', 'flip', 'brightness', 'contrast', 'saturation',
              'skill_path', 'project_path', 'resource_path', 'server', 'tool',
              'ref', 'selector', 'action', 'key', 'value', 'tab_id', 'mode', 'full_page', 'submit', 'expression')
PREVIEW_CHARS = 1200
# Result fields safe to show in activity listings (never file content).
RESULT_KEYS = ('job_id', 'status', 'exit_code', 'cwd', 'path', 'sha256', 'previous_sha256', 'bytes', 'total_chars', 'summary', 'lines_added', 'lines_removed',
               'server', 'tool', 'is_error', 'duration_seconds', 'url', 'title', 'tab_id', 'clicked', 'typed_into', 'action', 'target', 'elements', 'result_json', 'image_count', 'dialogs', 'load_timed_out', 'tiles', 'total_tiles', 'source_profile', 'copied', 'cancel_requested', 'cancellation_confirmed', 'call_settled',
               'next_offset', 'receipt_id', 'error', 'error_code', 'error_source', 'error_stage', 'input_kind', 'wrote_file',
               'output_truncated', 'timeout_seconds', 'network_enabled',
               'started_at', 'finished_at', 'truncated', 'scanned', 'output_strategy', 'stdout_bytes', 'stderr_bytes', 'detached',
               'source_format', 'original_width', 'original_height', 'oriented_width', 'oriented_height', 'width', 'height',
               'crop', 'frame_index', 'frames', 'mime_type', 'image_sha256', 'image_bytes',
               'source_path', 'source_sha256', 'source_bytes_preserved', 'output_format', 'file_id', 'download_host', 'requested_host', 'edits')
LIST_PREVIEW, DETAIL_PREVIEW = 3000, 65536


def _preview(text, limit=PREVIEW_CHARS):
    text = str(text)
    return text if len(text) <= limit else text[:limit] + '…'


class Harness:
    def __init__(self, files, jobs, storage, device_label='My Mac', step_cards=False, require_final_assessment=False):
        self.files, self.jobs, self.storage = files, jobs, storage
        self.device_label, self.step_cards = device_label, bool(step_cards)
        self.require_final_assessment = require_final_assessment
        self.lock = threading.RLock()
        self.bridge_controls = {}
        from result_images import ResultImages
        self.result_images = ResultImages(storage.path.parent)
        self.operations = {'list_directory': files.list, 'read_file': files.read, 'read_image': files.read_image,
                           'search_files': files.search, 'write_file': files.write,
                           'replace_text': files.replace, 'run_shell': jobs.run,
                           'job_status': jobs.status, 'cancel_job': jobs.cancel}
        with storage.lock:
            storage.db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)')
            storage.db.execute('CREATE TABLE IF NOT EXISTS task_actions (task_id TEXT, action_id TEXT, fingerprint TEXT, snapshot TEXT, PRIMARY KEY(task_id,action_id))')
            storage.db.execute('CREATE TABLE IF NOT EXISTS image_save_requests (request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, task_id TEXT NOT NULL)')
            # An uncertain mutation must be inspected, not automatically replayed.
            rows = storage.db.execute('SELECT task_id,action_id,snapshot FROM task_actions').fetchall()
            for task_id, action_id, body in rows:
                action = json.loads(body)
                if action['status'] == 'running':
                    recovered = storage.find_request(f'harness:{task_id}:{action_id}') if action['operation'] == 'run_shell' else None
                    if recovered and recovered[1].get('detached'):
                        action.update(status='succeeded', finished_at=time.time(), result=recovered[1])
                        storage.db.execute('UPDATE task_actions SET snapshot=? WHERE task_id=? AND action_id=?',
                                           (json.dumps(action), task_id, action_id))
                        continue
                    action.update(status='interrupted', finished_at=time.time(),
                                  result={'error': 'Agent restarted during this action; inspect actual state before retrying.'})
                    storage.db.execute('UPDATE task_actions SET snapshot=? WHERE task_id=? AND action_id=?',
                                       (json.dumps(action), task_id, action_id))
                    row = storage.db.execute('SELECT snapshot FROM tasks WHERE id=?', (task_id,)).fetchone()
                    if row:
                        task = json.loads(row[0]); task.update(status='paused', pause_reason='An action was interrupted by restart.')
                        storage.db.execute('UPDATE tasks SET snapshot=? WHERE id=?', (json.dumps(task), task_id))
            storage.db.commit()

    def _load(self, task_id):
        with self.storage.lock:
            row = self.storage.db.execute('SELECT snapshot FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            raise ToolError('未知 task_id。')
        return json.loads(row[0])

    def _save(self, task):
        with self.storage.lock:
            self.storage.db.execute('INSERT INTO tasks VALUES (?,?) ON CONFLICT(id) DO UPDATE SET snapshot=excluded.snapshot',
                                    (task['task_id'], json.dumps(task, ensure_ascii=False)))
            self.storage.db.commit()

    def _actions(self, task_id):
        with self.storage.lock:
            rows = self.storage.db.execute('SELECT snapshot FROM task_actions WHERE task_id=? ORDER BY rowid', (task_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _save_action(self, task_id, action_id, fingerprint, action):
        with self.storage.lock:
            self.storage.db.execute('INSERT INTO task_actions VALUES (?,?,?,?) ON CONFLICT(task_id,action_id) DO UPDATE SET snapshot=excluded.snapshot',
                                    (task_id, action_id, fingerprint, json.dumps(action, ensure_ascii=False)))
            self.storage.db.commit()

    @staticmethod
    def _plan(plan):
        items = [PlanItem.model_validate(item).model_dump() for item in plan]
        if not 1 <= len(items) <= 20 or len({p['id'] for p in items}) != len(items):
            raise ToolError('计划需要 1–20 个 ID 唯一的步骤。')
        if sum(p['status'] == 'in_progress' for p in items) > 1:
            raise ToolError('最多只能有一个 in_progress 步骤。')
        return items

    @staticmethod
    def _active(task):
        if task['status'] != 'active':
            raise ToolError('任务已暂停或结束；不得继续执行新动作。')
        if time.time() >= task['deadline']:
            raise ToolError('任务时间上限已到；保存进度并停止。')
        if task['action_count'] >= task['max_actions']:
            raise ToolError('任务动作上限已到；保存进度并停止。')

    def _live_result(self, action):
        """Shell receipts point at a job; report the job's current snapshot rather than the launch-time one."""
        result = dict(action.get('result', {}))
        if action['operation'] in ('run_shell', 'job_status', 'cancel_job') and result.get('job_id'):
            try:
                result = self.jobs.status(result['job_id'])
            except ToolError:
                pass
        return result

    @staticmethod
    def _outcome(action, result):
        """One word for a receipt: the job outcome for shell actions, else the action status."""
        if action['status'] in ('failed', 'interrupted', 'running'):
            return action['status']
        if result.get('is_error'):
            return 'failed'
        if action['operation'] in ('run_shell', 'job_status', 'cancel_job') and result.get('status'):
            return result['status']
        return action['status']

    def create(self, workspace, objective, plan, checks, max_actions=1000, max_continuations=80, max_minutes=240, *, image_save=None):
        self.files.path(workspace)
        if not 1 <= len(objective.strip()) <= 4000 or not 1 <= len(checks) <= 12:
            raise ToolError('目标不能为空，最多 4000 字符；需要 1–12 个明确验收项。')
        if not 1 <= max_actions <= 10000 or not 0 <= max_continuations <= 500 or not 1 <= max_minutes <= 1440:
            raise ToolError('任务上限不合法。')
        parsed = [Check.model_validate(c).model_dump() for c in checks]
        for c in parsed:
            if c['kind'] == 'job_succeeded':
                if not c['action_id']:
                    raise ToolError('job_succeeded 必须指定未来验证命令的 action_id。')
            elif c['kind'] == 'image_saved':
                if not c['path'] or not c['action_id']:
                    raise ToolError('image_saved 必须指定输出 path 和保存动作 action_id。')
                self.files.path(workspace, c['path'])
            else:
                if not c['path'] or c['value'] is None or (c['kind'] != 'file_equals' and not c['value']):
                    raise ToolError('文件验收必须有 path 和有效 value。')
                self.files.path(workspace, c['path'])
                if c['kind'] == 'file_sha256' and (len(c['value']) != 64 or any(x not in '0123456789abcdef' for x in c['value'])):
                    raise ToolError('SHA-256 格式不合法。')
        now = time.time()
        task = {'task_id': uuid.uuid4().hex, 'objective': objective, 'workspace': workspace,
                'plan': self._plan(plan), 'checks': parsed, 'status': 'active', 'revision': 1,
                'created_at': now, 'updated_at': now, 'finished_at': None, 'deadline': now + max_minutes * 60,
                'max_actions': max_actions, 'action_count': 0, 'max_continuations': max_continuations,
                'continuations': 0, 'last_continued_revision': None, 'last_mutation_at': 0,
                'pause_reason': None, 'last_decision': None, 'auto_continue': False, 'observation_count': 0,
                'require_final_assessment': self.require_final_assessment, 'assessment': None}
        with self.lock:
            if image_save is not None:
                task['image_save'] = image_save
                task['auto_continue'] = True
                if image_save.get('generation_prompt'):
                    task['image_workflow'] = {'version': 2, 'messages': []}
            self._save(task)
        return self.get(task['task_id'])

    def prepare_image_save(self, workspace, path, objective, request_id, expected_sha256=None, create_parents=False, *, reference_source=None, generation_prompt=None):
        """Register the save before native generation can end the Chat turn."""
        root, parts = self.files.path(workspace, path)
        if not parts or root.joinpath(*parts).suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
            raise ToolError('请指定 .png、.jpg、.jpeg 或 .webp 图片输出路径。')
        if not 1 <= len(request_id) <= 100:
            raise ToolError('request_id 需要 1–100 个字符；重试使用相同值。')
        path = str(root.joinpath(*parts))
        bound = {'path': path, 'expected_sha256': expected_sha256, 'create_parents': bool(create_parents),
                 'save_action_id': 'save-image', 'continuation_idle_seconds': 180}
        if generation_prompt is not None:
            if not isinstance(generation_prompt, str) or not 1 <= len(generation_prompt.strip()) <= 4000:
                raise ToolError('图片创作要求需要 1–4000 个字符。')
            bound['generation_prompt'] = generation_prompt.strip()
        plan = [{'id': 'save', 'step': '用 ChatGPT 原生生图生成图片，并保存同一张成图到指定本机路径。', 'status': 'in_progress'},
                {'id': 'verify', 'step': '读取本机成图，核对画面及原始文件字节一致。', 'status': 'pending'}]
        checks = [{'kind': 'image_saved', 'path': path, 'action_id': 'save-image'}]
        if reference_source is not None:
            if reference_source['path'] == path:
                raise ToolError('原生图片编辑先另存新文件，参考原图必须保留。')
            bound.update(reference_source=reference_source, reference_bound=False)
            plan[0]['status'] = 'pending'
            plan.insert(0, {'id': 'reference', 'step': '把指定原图传为 ChatGPT 文件，并校验参考图字节。', 'status': 'in_progress'})
            checks.append({'kind': 'file_sha256', 'path': reference_source['path'], 'value': reference_source['sha256']})
        fingerprint = hashlib.sha256(json.dumps([workspace, objective, bound], sort_keys=True).encode()).hexdigest()
        with self.lock, self.storage.lock:
            old = self.storage.db.execute('SELECT fingerprint,task_id FROM image_save_requests WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise ToolError('request_id 已用于另一个保存目标；不能改写既有待办。')
                return self.get(old[1])
            task = self.create(workspace, objective, plan, checks,
                               max_actions=12, max_continuations=3, max_minutes=30, image_save=bound)
            self.storage.db.execute('INSERT INTO image_save_requests VALUES (?,?,?)', (request_id, fingerprint, task['task_id']))
            self.storage.db.commit()
            return task

    def get(self, task_id):
        with self.lock:
            task = self._load(task_id)
            now = time.time()
            task['effective_status'] = ('budget_limited' if task['status'] == 'active' and
                                       (now >= task['deadline'] or task['action_count'] >= task['max_actions']) else task['status'])
            task['next_step'] = next((p for p in task['plan'] if p['status'] == 'in_progress'),
                                     next((p for p in task['plan'] if p['status'] == 'pending'), None))
            actions = self._actions(task_id)
            task['recent_actions'] = [self._summary(a) for a in actions[-6:]]
            # Per-step execution facts for the panel. Plan items themselves stay exactly as submitted,
            # because update_plan rejects unknown fields echoed back from here.
            stats = {}
            for action in actions:
                result = self._live_result(action)
                outcome = self._outcome(action, result)
                entry = stats.setdefault(action['step_id'], {'actions': 0, 'failed': 0, 'last': None})
                entry['actions'] += 1
                if outcome in ('failed', 'timed_out', 'interrupted', 'cancelled') or (result.get('exit_code') not in (None, 0)):
                    entry['failed'] += 1
                entry['last'] = {'action_id': action['action_id'], 'operation': action['operation'], 'outcome': outcome,
                                 'exit_code': result.get('exit_code'), 'finished_at': action.get('finished_at')}
            task['step_stats'] = stats
            # Live acceptance status without writing audit receipts; review_task/finish_task keep recording.
            report = self.review(task_id, record=False)
            task['checks_status'] = {'decision': report['decision'], 'running_jobs': report['running_jobs'],
                                     'passed': sum(c['passed'] for c in report['checks']), 'total': len(report['checks']),
                                     'checks': [{'passed': c['passed'], 'evidence': c['evidence']} for c in report['checks']]}
            task['idle_seconds'] = max(0, round(now - task['updated_at'], 1))
            # Older terminal snapshots have only updated_at. Keep their persisted end time as-is.
            end = (task.get('finished_at') or task['updated_at']) if task['status'] in ('complete', 'cancelled') else now
            task['elapsed_seconds'] = max(0, round(end - task['created_at']))
            task['remaining_seconds'] = max(0, round(task['deadline'] - now))
            task['device_label'] = self.device_label
            task['features'] = {'step_cards': self.step_cards}
            task['permission_mode'] = 'full_machine' if self.files.full_machine else 'workspace'
            task['completion_assessment_required'] = bool(task.get('require_final_assessment'))
            task['protocol'] = {'completion_tool': 'finish_task', 'review_tool': 'review_task',
                                'requires_completed_plan': True, 'requires_passed_checks': True,
                                'task_state_grants_authorization': False}
            if task.get('image_save'):
                task['initial_image_checkpoint'] = task['continuations'] == 0 and task['action_count'] == 0
                saves = [a for a in actions if a['operation'] == 'save_chat_image' and
                         (a['action_id'] == 'save-image' or a['action_id'].startswith('save-image:'))]
                latest = saves[-1] if saves else None
                transient = latest and latest['status'] == 'failed' and latest['result'].get('wrote_file') is False and \
                    latest['result'].get('error_code') in ('IMAGE_DOWNLOAD_TIMEOUT', 'IMAGE_DOWNLOAD_TEMPORARY')
                retry = bool(transient and len(saves) < 3 and task['effective_status'] == 'active')
                save_id = latest['action_id'] if latest else 'save-image'
                if retry:
                    number = 2
                    used = {a['action_id'] for a in saves}
                    while 'save-image:' + str(number) in used:
                        number += 1
                    save_id = 'save-image:' + str(number)
                task['image_save_retry'] = {'available': retry, 'attempts': len(saves), 'max_attempts': 3,
                                            'same_generated_file_only': True, 'new_generation_required': False}
                task['image_save_binding'] = {'task_id': task_id, 'step_id': 'save', 'action_id': save_id,
                                              'workspace': task['workspace'],
                                              **{key: task['image_save'][key] for key in ('path', 'expected_sha256', 'create_parents')}}
                task['protocol']['save_tool'] = 'save_chat_image'
                task['protocol']['continuation_requires_live_component'] = True
                task['protocol']['image_acceptance'] = 'native file saved byte-for-byte, then read back from the same local path'
            return public_receipt(task)

    COMPACT_KEYS = ('task_id', 'workspace', 'status', 'effective_status', 'revision', 'action_count', 'max_actions',
                    'observation_count', 'continuations', 'max_continuations', 'remaining_seconds', 'idle_seconds',
                    'next_step', 'plan', 'step_stats', 'auto_continue', 'last_mutation_at', 'image_save', 'image_workflow')

    def compact(self, task_id):
        """Step results echo only what the next decision needs; get_task returns the objective, checks and evidence."""
        task = self.get(task_id)
        out = {k: task[k] for k in self.COMPACT_KEYS if task.get(k) is not None}
        # Image flows read their bindings and retry state from step results; those dicts are small.
        out.update({k: v for k, v in task.items() if k.startswith('image_') and v is not None})
        status = task.get('checks_status') or {}
        out['checks_status'] = {'decision': status.get('decision'), 'passed': status.get('passed'), 'total': status.get('total'),
                                'running_jobs': status.get('running_jobs', []), 'checks': [{'passed': c['passed'], 'evidence': c.get('evidence')} for c in status.get('checks', [])]}
        out['note'] = 'Compact task view. get_task returns the objective, acceptance checks, receipts and evidence.'
        return out

    def _summary(self, action):
        result = self._live_result(action)
        item = {k: action.get(k) for k in ('action_id', 'step_id', 'operation', 'status', 'started_at', 'finished_at')}
        item['outcome'] = self._outcome(action, result)
        item['exit_code'] = result.get('exit_code')
        inputs = action.get('inputs', {})
        item['inputs'] = {k: inputs[k] for k in ('command', 'cwd', 'path', 'job_id') if k in inputs}
        return item

    def _record(self, action, preview_limit):
        item = {k: action.get(k) for k in ('action_id', 'step_id', 'operation', 'status', 'started_at', 'finished_at', 'inputs')}
        result = self._live_result(action)
        item['outcome'] = self._outcome(action, result)
        # A shell launch returns after at most 0.5s; its action timestamps are not the job duration.
        timing = result if action['operation'] == 'run_shell' and result.get('job_id') else action
        finished, started = timing.get('finished_at'), timing.get('started_at')
        item['duration_seconds'] = max(0, round(finished - started, 3)) if finished is not None and started is not None else None
        # Avoid copying file bodies into activity; shell output is bounded for the panel.
        item['result'] = {k: result[k] for k in RESULT_KEYS if k in result}
        for key, value in result.items():
            if isinstance(value, list) and key in ('entries', 'results', 'files'):
                item['result'][f'{key}_count'] = len(value)
        for key in ('stdout', 'stderr', 'text', 'snapshot'):
            if key in result and isinstance(result[key], str):
                item['result'][key] = result[key][:preview_limit]
                if len(result[key]) > preview_limit:
                    item['result']['preview_truncated'] = True
        return public_receipt(item)

    def activity(self, task_id, limit=20, action_id=None):
        if not 1 <= limit <= 50:
            raise ToolError('记录数量必须在 1–50 之间。')
        with self.lock:
            task = self._load(task_id)
            actions = self._actions(task_id)
            if action_id is not None:
                action = next((a for a in actions if a['action_id'] == action_id), None)
                if action is None:
                    raise ToolError('本任务没有这个 action_id。')
                return {'task_id': task_id, 'total': len(actions), 'records': [self._record(action, DETAIL_PREVIEW)],
                        'last_decision': task.get('last_decision'), 'note': '单个动作的完整回执（输出以本机保存的上限为准）。'}
            records = [self._record(action, LIST_PREVIEW) for action in reversed(actions[-limit:])]
            return {'task_id': task_id, 'total': len(actions), 'records': records, 'last_decision': task.get('last_decision'),
                    'note': '执行记录及模型提交的简短计划说明；不包含模型内部思考过程。'}

    def plan(self, task_id, plan, expected_revision, explanation):
        with self.lock:
            task = self._load(task_id)
            if task['status'] != 'active':
                raise ToolError('任务已暂停或结束。')
            if task['revision'] != expected_revision:
                raise ToolError('任务版本已变化；先 get_task，再基于最新状态更新计划。')
            task.update(plan=self._plan(plan), revision=task['revision'] + 1, assessment=None,
                        updated_at=time.time(), last_decision=explanation[:1500])
            self._save(task)
        return self.compact(task_id)

    @staticmethod
    def _inputs(operation, arguments):
        inputs = {k: arguments[k] for k in INPUT_KEYS if k in arguments}
        if operation == 'write_file' and isinstance(arguments.get('content'), str):
            inputs['content_chars'] = len(arguments['content'])
            inputs['content_preview'] = _preview(arguments['content'])
        if operation == 'replace_text':
            for key in ('old_text', 'new_text'):
                if isinstance(arguments.get(key), str):
                    inputs[f'{key}_chars'] = len(arguments[key])
                    inputs[key] = _preview(arguments[key])
        if operation == 'apply_patch' and isinstance(arguments.get('patch'), str):
            inputs['patch_chars'] = len(arguments['patch'])
            inputs['patch_preview'] = _preview(arguments['patch'], 3000)
            try:
                from patching import parse_patch
                inputs['patch_files'] = [f"{op['path']} ({op['kind']})" for op in parse_patch(arguments['patch'])][:20]
            except Exception:
                pass
        if operation == 'call_mcp_tool' and isinstance(arguments.get('arguments'), dict):
            inputs['arguments_preview'] = _preview(json.dumps(arguments['arguments'], ensure_ascii=False))
        if operation == 'browser_navigate' and isinstance(arguments.get('url'), str):
            inputs['url'] = arguments['url'][:300]  # download/save URLs stay out of receipts; page URLs are the action itself
        if operation in ('browser_click', 'browser_act') and isinstance(arguments.get('text'), str):
            inputs['text'] = _preview(arguments['text'], 200)
        if operation == 'browser_type' and isinstance(arguments.get('text'), str):
            inputs['text_chars'] = len(arguments['text'])  # typed text may be a credential; only its length is recorded
        return inputs

    def execute(self, task_id, step_id, operation, arguments, action_id, *, observe=False, plan=None, expected_revision=None, explanation=''):
        if operation not in self.operations or not 1 <= len(action_id) <= 80:
            raise ToolError('operation 或 action_id 不合法。')
        identity = arguments
        if operation == 'save_chat_image' and isinstance(arguments.get('file'), dict) and arguments['file'].get('file_id'):
            # Signed URLs and display names may change when the host rebinds the
            # same native file. Replaying its action must retrieve the receipt,
            # never redownload or overwrite under a refreshed URL.
            identity = {**arguments, 'file': {'file_id': arguments['file']['file_id']}}
        fingerprint = hashlib.sha256(json.dumps([step_id, operation, identity], sort_keys=True).encode()).hexdigest()
        legacy_fingerprint = hashlib.sha256(json.dumps([step_id, operation, arguments], sort_keys=True).encode()).hexdigest()
        with self.lock:
            if observe and operation not in ('get_project_context', 'find_skills', 'read_local_skill', 'read_file', 'read_image', 'list_directory', 'search_files', 'job_status', 'list_mcp_tools', 'browser_snapshot', 'browser_screenshot'):
                raise ToolError('只读步骤不能执行写入、shell 或取消操作。')
            task = self._load(task_id)
            with self.storage.lock:
                old = self.storage.db.execute('SELECT fingerprint,snapshot FROM task_actions WHERE task_id=? AND action_id=?', (task_id, action_id)).fetchone()
            if old:
                if old[0] not in (fingerprint, legacy_fingerprint):
                    raise ToolError('action_id 已用于不同动作；拒绝复用。')
                action = json.loads(old[1])
                action['result'] = self._live_result(action)
                payload = {'replayed': True, 'action': action, 'task': self.compact(task_id)}
                refs = action['result'].get('image_attachments')
                if refs:
                    payload['images'] = self.result_images.load(refs)
                elif action['result'].get('image_count'):
                    raise ToolError('旧版动作未保存图片附件，无法重放原画面；原动作没有重新执行。')
                return payload
            if observe:
                if task['status'] != 'active':
                    raise ToolError('任务已暂停或结束。')
            else:
                self._active(task)
            if plan is not None:
                if observe or expected_revision != task['revision']:
                    raise ToolError(f'合并计划更新需要当前 expected_revision={task["revision"]}，收到 {expected_revision}。只读步骤也会更新版本；使用最近一次工具结果的 task.revision，状态不清楚时先 get_task。只读工具不能附带计划更新。')
                task.update(plan=self._plan(plan), last_decision=explanation[:1500], assessment=None)
            if any(a['status'] == 'running' for a in self._actions(task_id)):
                raise ToolError('该任务已有动作正在执行，请等待其回执。')
            if not any(p['id'] == step_id and p['status'] == 'in_progress' for p in task['plan']):
                raise ToolError('先 update_plan 将目标步骤设为唯一 in_progress。')
            args = dict(arguments)
            bound = task.get('image_save')
            if bound and operation in MUTATIONS:
                if bound.get('reference_source') and not bound.get('reference_bound'):
                    raise ToolError('原图尚未完成 ChatGPT 文件绑定与字节校验，不能保存未经参考图确认的成图。')
                if operation != 'save_chat_image':
                    raise ToolError('该成图保存待办只允许保存原生成文件，不能用其他写入或 shell 替代。')
                root, parts = self.files.path(task['workspace'], args.get('path', ''))
                if str(root.joinpath(*parts)) != bound['path'] or args.get('expected_sha256') != bound['expected_sha256'] or bool(args.get('create_parents')) != bound['create_parents']:
                    raise ToolError('保存路径或覆盖设置与已登记的成图待办不一致。')
            if 'workspace' in args or 'request_id' in args:
                raise ToolError('workspace 和 shell request_id 由任务控制器绑定，请不要传入。')
            if operation in ('job_status', 'cancel_job'):
                owned = {a.get('result', {}).get('job_id') for a in self._actions(task_id) if a['operation'] == 'run_shell'}
                if args.get('job_id') not in owned or not args.get('job_id'):
                    raise ToolError('只能操作本任务启动的 shell job。')
            elif operation not in BRIDGE_OPERATIONS:
                args['workspace'] = task['workspace']
            if operation == 'run_shell':
                args['request_id'] = f'harness:{task_id}:{action_id}'
                # A long command may use the remaining task time, but cannot exceed its task deadline.
                remaining = max(1, int(task['deadline'] - time.time()))
                default_timeout = self.jobs.settings.get('default_shell_timeout_seconds', 7200) if self.jobs.settings.get('durable_jobs') else 60
                args['timeout_seconds'] = min(args.get('timeout_seconds') or default_timeout, remaining)
            if operation in CONTROLLED_OPERATIONS:
                if any(key.startswith('_') for key in args):
                    raise ToolError('工具取消和时间限制参数由任务控制器管理。')
                from mcp_bridge import CallControl
                control = CallControl(scope=task_id)
                args.update(_control=control, _deadline=task['deadline'])
            try:
                inspect.signature(self.operations[operation]).bind(**args)
            except TypeError as exc:
                raise ToolError(str(exc)) from exc
            now = time.time()
            action = {'action_id': action_id, 'step_id': step_id, 'operation': operation,
                      'status': 'running', 'started_at': now, 'finished_at': None, 'result': {},
                      'inputs': self._inputs(operation, arguments)}
            task.update(action_count=task['action_count'] + (0 if observe else 1),
                        observation_count=task.get('observation_count', 0) + (1 if observe else 0),
                        revision=task['revision'] + 1, updated_at=now)
            if operation in MUTATIONS:
                task['last_mutation_at'] = now
                task['assessment'] = None
            self._save_action(task_id, action_id, fingerprint, action); self._save(task)
            if operation in CONTROLLED_OPERATIONS:
                self.bridge_controls[(task_id, action_id)] = control
        images = None
        unexpected_failure = False
        try:
            action['result'] = self.operations[operation](**args)
            action['status'] = 'failed' if action['result'].get('is_error') else 'succeeded'
            if isinstance(action['result'], dict) and action['result'].get('images'):
                # Persist immutable attachments before committing the receipt.
                # SQLite stores references, never base64 image payloads.
                images = action['result'].pop('images')
                action['result']['image_attachments'] = self.result_images.save(images)
                action['result']['image_count'] = len(images)
                images = self.result_images.load(action['result']['image_attachments'])
        except (ToolError, TypeError, ValueError, OSError) as exc:
            result = {'error': str(exc)[:2000]}
            diagnostics = getattr(exc, 'receipt', {})
            for key in ('error_code','error_source','error_stage','input_kind','wrote_file','cancel_requested','cancellation_confirmed','call_settled'):
                if key in diagnostics:
                    result[key] = diagnostics[key]
            action.update(status='failed', result=result)
            images = None
            if result.get('error_code') in ('MCP_CANCELLED', 'BROWSER_CANCELLED', 'BROWSER_TIMEOUT', 'IMAGE_CANCELLED', 'IMAGE_TIMEOUT'):
                action['status'] = 'cancelled' if result.get('cancellation_confirmed') else 'interrupted'
        except Exception as exc:
            # Never leave a receipt permanently running after an operation
            # crashes. Its side effects are unknown, so pause for inspection.
            unexpected_failure = True
            action.update(status='interrupted', result={
                'error': f'操作遇到未预期异常（{type(exc).__name__}）；请检查实际文件或远端状态后再恢复任务。',
                'error_code': 'OPERATION_UNCERTAIN', 'error_type': type(exc).__name__})
            images = None
        finally:
            with self.lock:
                task = self._load(task_id)
                if unexpected_failure and task['status'] == 'active':
                    task.update(status='paused', auto_continue=False, pause_reason=action['result']['error'])
                control = self.bridge_controls.pop((task_id, action_id), None)
                if control and control.requested.is_set():
                    task['mcp_cancellation'] = [{'action_id': action_id, **control.summary()}]
                if task['status'] != 'active' and operation == 'run_shell' and action['result'].get('job_id'):
                    action['result'] = self.jobs.cancel(action['result']['job_id'])
                action['finished_at'] = time.time()
                self._save_action(task_id, action_id, fingerprint, action)
                task.update(revision=task['revision'] + 1, updated_at=time.time())
                self._save(task)
        payload = {'replayed': False, 'action': action, 'task': self.compact(task_id)}
        if images:
            payload['images'] = images
        return payload

    def review(self, task_id, record=True):
        read = self.files.read if record else self.files.peek
        with self.lock:
            task = self._load(task_id); actions = {a['action_id']: a for a in self._actions(task_id)}
            results = []
            running = []
            for action in actions.values():
                if action['status'] == 'running':
                    running.append(action['action_id'])
                if action['operation'] == 'run_shell' and action['result'].get('job_id'):
                    job = self.jobs.status(action['result']['job_id'])
                    if job['status'] not in TERMINAL_JOBS:
                        running.append(job['job_id'])
            for check in task['checks']:
                passed, evidence = False, None
                try:
                    if check['kind'] == 'job_succeeded':
                        # A failed verification can be rerun as verify:2 without changing its criterion.
                        # Only shell launches count; a job_status receipt named verify:2:status must not shadow them.
                        attempts = [a for a in actions.values() if a['operation'] == 'run_shell' and
                                    (a['action_id'] == check['action_id'] or a['action_id'].startswith(check['action_id'] + ':'))]
                        action = attempts[-1] if attempts else None
                        if not action or not action['result'].get('job_id'):
                            raise ToolError('缺少本任务对应验证命令的执行回执。')
                        job = self.jobs.status(action['result']['job_id'])
                        duration = (job.get('finished_at') or time.time()) - job['started_at']
                        passed = (job['status'] == 'completed' and job['exit_code'] == 0 and job['started_at'] >= task['last_mutation_at']
                                  and duration >= check.get('min_duration_seconds', 0)
                                  and (not check.get('value') or check['value'] in job.get('stdout', '')))
                        evidence = {k: job.get(k) for k in ('job_id', 'status', 'exit_code', 'receipt_id')}
                        evidence['duration_seconds'] = round(duration, 3)
                        evidence['required_duration_seconds'] = check.get('min_duration_seconds', 0)
                        evidence['output_matched'] = not check.get('value') or check['value'] in job.get('stdout', '')
                        evidence['action_id'] = action['action_id']
                        if job['started_at'] < task['last_mutation_at']:
                            evidence['stale'] = True
                    elif check['kind'] == 'image_saved':
                        attempts = [a for a in actions.values() if a['operation'] == 'save_chat_image' and
                                    (a['action_id'] == check['action_id'] or a['action_id'].startswith(check['action_id'] + ':'))]
                        action = attempts[-1] if attempts else None
                        if not action or action['status'] != 'succeeded':
                            raise ToolError('尚未收到原生成图成功保存的回执。')
                        saved = action['result']
                        content = self.files.fingerprint(task['workspace'], check['path'], record=record)
                        matching = (saved.get('path') == content['path'] and saved.get('sha256') == content['sha256']
                                    and saved.get('source_bytes_preserved') is True and saved.get('source_sha256') == content['sha256'])
                        reads = [a for a in actions.values() if a['operation'] == 'read_image' and a['status'] == 'succeeded'
                                 and a['started_at'] >= action['finished_at'] and a['result'].get('path') == content['path']
                                 and a['result'].get('sha256') == content['sha256']]
                        passed = matching and bool(reads)
                        evidence = {'path': content['path'], 'sha256': content['sha256'], 'file_id': saved.get('file_id'),
                                    'action_id': action['action_id'], 'source_bytes_preserved': matching,
                                    'read_back_action_id': reads[-1]['action_id'] if reads else None,
                                    'receipt_id': saved.get('receipt_id')}
                    else:
                        content = (self.files.fingerprint(task['workspace'], check['path'], record=record)
                                   if check['kind'] == 'file_sha256' else read(task['workspace'], check['path'], 0, 64000))
                        if check['kind'] == 'file_sha256':
                            passed = content['sha256'] == check['value']
                        elif content['next_offset'] is not None:
                            raise ToolError('文本较长，请用 SHA-256 验收。')
                        elif check['kind'] == 'file_equals':
                            passed = content['content'] == check['value']
                        else:
                            passed = check['value'] in content['content']
                        evidence = {'path': content['path'], 'sha256': content['sha256'], 'receipt_id': content['receipt_id']}
                except ToolError as exc:
                    evidence = {'error': str(exc)}
                results.append({'check': check, 'passed': passed, 'evidence': evidence})
            pending = [p for p in task['plan'] if p['status'] != 'completed']
            verified = all(c['passed'] for c in results) and not pending and not running
            assessed = not task.get('require_final_assessment') or bool(task.get('assessment'))
            gaps = (task.get('assessment') or {}).get('remaining_work', [])
            ready = verified and assessed and not gaps
            decision = 'complete' if ready else 'wait' if running else 'review' if verified and not assessed else 'continue'
            if task['status'] != 'active':
                decision = task['status']
            elif not ready and (time.time() >= task['deadline'] or task['action_count'] >= task['max_actions']):
                decision = 'budget_limited'
            return public_receipt({'task_id': task_id, 'revision': task['revision'], 'decision': decision,
                    'can_complete': ready and task['status'] == 'active', 'checks': results,
                    'remaining_steps': pending, 'running_jobs': running, 'remaining_work': gaps,
                    'assessment_required': not assessed,
                    'completion_tool': 'finish_task', 'shell_verification_requires_no_later_mutation': True})

    def finish(self, task_id, assessment=None):
        with self.lock:
            task = self._load(task_id)
            if task['status'] == 'complete':
                return {'completed': True, 'task': self.get(task_id)}
            if assessment is not None:
                parsed = CompletionAssessment.model_validate(assessment).model_dump()
                actions = {a['action_id']: a for a in self._actions(task_id)}
                if any(i not in actions for i in parsed['evidence_action_ids']):
                    raise ToolError('复核引用了不属于本任务的动作回执。')
                task['assessment'] = parsed
                self._save(task)
            report = self.review(task_id)
            receipt = self.storage.event('finish_task', {'task_id': task_id, 'completed': report['can_complete'], 'decision': report['decision']})
            if not report['can_complete']:
                return {'completed': False, 'receipt_id': receipt, 'review': report, 'task': self.get(task_id)}
            now = time.time()
            task.update(status='complete', revision=task['revision'] + 1, updated_at=now, finished_at=now, final_review=report, auto_continue=False)
            self._save(task)
            return {'completed': True, 'receipt_id': receipt, 'task': self.get(task_id)}

    def set_state(self, task_id, status, reason):
        controls = []
        with self.lock:
            task = self._load(task_id)
            if status not in ('active','paused','cancelled') or task['status'] in ('complete','cancelled'):
                raise ToolError('不能执行这个状态变更。')
            if status == 'active' and (time.time() >= task['deadline'] or task['action_count'] >= task['max_actions']):
                raise ToolError('任务预算已到；不能静默重置上限。')
            now = time.time()
            task.update(status=status, pause_reason=reason[:1000], revision=task['revision'] + 1, updated_at=now)
            if status != 'active':
                task['auto_continue'] = False
                controls = [(aid, c) for (tid, aid), c in self.bridge_controls.items()
                            if tid == task_id and not c.settled.is_set()]
                for _, control in controls:
                    control.cancel()
                if controls:
                    task['mcp_cancellation'] = [{'action_id': aid, **c.summary()} for aid, c in controls]
            else:
                task.pop('mcp_cancellation', None)
            if status == 'cancelled':
                task['finished_at'] = now
            self._save(task)
            if status != 'active':
                for action in self._actions(task_id):
                    job_id = action.get('result', {}).get('job_id')
                    if action['operation'] == 'run_shell' and job_id:
                        self.jobs.cancel(job_id)
        # The call's finally block needs the harness lock to persist its receipt.
        # Never hold that lock while waiting for cancellation to settle.
        if controls:
            until = time.monotonic() + 5
            for _, control in controls:
                control.settled.wait(max(0, until - time.monotonic()))
            with self.lock:
                task = self._load(task_id)
                task['mcp_cancellation'] = [{'action_id': aid, **c.summary()} for aid, c in controls]
                task.update(revision=task['revision'] + 1, updated_at=time.time())
                self._save(task)
        return self.get(task_id)

    def automation(self, task_id, enabled):
        with self.lock:
            task = self._load(task_id)
            if enabled:
                self._active(task)
            task['auto_continue'] = bool(enabled)
            self._save(task)
        return self.get(task_id)

    def claim_continuation(self, task_id, expected_revision):
        with self.lock:
            task = self._load(task_id); self._active(task)
            if task.get('image_workflow'):
                return {'send': False, 'reason': 'This image workflow owns its two bounded messages; another component cannot duplicate them.'}
            if task['revision'] != expected_revision:
                return {'send': False, 'reason': 'Task progressed; refresh before continuing.'}
            if task['last_continued_revision'] == expected_revision:
                return {'send': False, 'reason': 'Continuation already sent for this checkpoint; no automatic repeat without progress.'}
            if task['continuations'] >= task['max_continuations']:
                return {'send': False, 'reason': 'Continuation limit reached.'}
            if self.review(task_id, record=False)['running_jobs']:
                return {'send': False, 'reason': 'A task command is still running.'}
            task.update(last_continued_revision=expected_revision, continuations=task['continuations'] + 1)
            self._save(task)
            if task.get('image_save'):
                return {'send': True, 'task_id': task_id, 'sequence': task['continuations'],
                        'prompt': f'[LocalPilot 成图保存续接 {task_id}]\n原请求：{task["objective"]}\n'
                                  '请继续这项已登记的图片保存请求。任务状态可通过 get_task 查询。'
                                  '已有成图只需要保存和本机读回，不重新生成；保存工具为 save_chat_image。'
                                  '本消息不增加权限，用户最新要求和宿主文件传递权限保持有效。'}
            return {'send': True, 'task_id': task_id, 'sequence': task['continuations'],
                    'prompt': f'[LocalPilot 任务续跑 {task_id}，第 {task["continuations"]} 次]\n'
                              '继续这个既有任务，先调用 get_task 恢复最新目标与计划。使用 run_task_step 执行，更新计划，并用 review_task 和 finish_task 验收。'
                              '保持原目标和验收条件；命令结束不等于目标完成。只在任务完成、用户停止或存在需要用户处理的阻塞时结束。'
                              '仍使用普通 Chat 和 LocalPilot，不调用 Work、Codex 或其他模型 API。本消息不增加原任务权限；用户最新的停止或变更要求优先。'}
