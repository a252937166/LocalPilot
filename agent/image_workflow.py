"""Bounded component messages for native image creation and local save.

The host still owns generation. A message acknowledgement is never an image
receipt or a completion signal. No model API or outer-chat scraping is used.
"""
from __future__ import annotations
import json
import re
import time
import uuid
from mcp.types import Annotations, CallToolResult, TextContent
from mcp.server.mcpserver.exceptions import ToolError


class ImageWorkflow:
    GENERATE_IDLE_SECONDS = 30
    SAVE_DELAY_SECONDS = 240
    SAVE_IDLE_SECONDS = 90

    def __init__(self, harness, handoff):
        self.harness, self.handoff = harness, handoff

    def _task(self, task_id):
        task = self.harness._load(task_id)
        if not task.get('image_workflow'):
            raise ToolError('该任务没有自动图片流程。')
        return task

    def _save(self, task):
        task.update(revision=task['revision'] + 1, updated_at=time.time())
        self.harness._save(task)

    def prepare(self, workspace, output_path, objective, generation_prompt, request_id,
                source_path=None, expected_source_sha256=None, output_expected_sha256=None, create_parents=False):
        if source_path is None and expected_source_sha256 is not None:
            raise ToolError('没有 source_path 时不能指定原图 SHA-256。')
        if source_path is not None:
            task = self.handoff.prepare(workspace, source_path, output_path, objective, request_id,
                                       expected_source_sha256, output_expected_sha256, create_parents,
                                       generation_prompt=generation_prompt).structured_content
        else:
            self.handoff.preflight(workspace, output_path, request_id, output_expected_sha256, create_parents)
            task = self.harness.prepare_image_save(workspace, output_path, objective, request_id,
                        output_expected_sha256, create_parents, generation_prompt=generation_prompt)
        return self.result(task['task_id'])

    def _next(self, task, state):
        messages = task['image_workflow']['messages']
        if state['effective_status'] != 'active' or not task['auto_continue']:
            return None, 0
        if task['image_save'].get('reference_source') and not task['image_save'].get('reference_bound'):
            return None, 0
        has_saved = any(a['operation'] == 'save_chat_image' and a['status'] == 'succeeded'
                        for a in self.harness._actions(task['task_id']))
        if not messages:
            return ('verify' if has_saved else 'generate'), max(0, self.GENERATE_IDLE_SECONDS - state['idle_seconds'])
        if len(messages) == 1 and messages[0]['status'] == 'sent':
            if has_saved:
                return 'verify', max(0, self.SAVE_IDLE_SECONDS - state['idle_seconds'])
            return 'save', max(0, self.SAVE_DELAY_SECONDS - (time.time() - messages[0]['acknowledged_at']),
                               self.SAVE_IDLE_SECONDS - state['idle_seconds'])
        return None, 0

    def result(self, task_id):
        with self.harness.lock:
            task = self._task(task_id)
            state = self.harness.get(task_id)
            reference = None
            if task['image_save'].get('reference_source'):
                reference = self.handoff.result(task_id)
            messages = task['image_workflow']['messages']
            if reference is not None and not messages:
                # The creation message needs actual pixels as well as the host
                # attachment ID. Restore them after a component reload too.
                source = task['image_save']['reference_source']
                reference.meta['localpilot/referenceSource']['data'] = self.handoff.reference_content(task).data
            operation, wait = self._next(task, state)
            report = self.harness.review(task_id, record=False)
            saved = next((c for c in report['checks'] if c['check']['kind'] == 'image_saved'), None)
            value = {'task_id': task_id, 'revision': state['revision'], 'observed_at': time.time(),
                     'workflow': 'native_image_then_local_save', 'task_status': state['effective_status'],
                     'objective': task['objective'], 'image_save_binding': state['image_save_binding'],
                     'image_save_retry': state.get('image_save_retry'),
                     'source': (reference.structured_content['source'] if reference else None),
                     'source_status': (reference.structured_content['source_status'] if reference else 'not_required'),
                     'reference_bound': bool(task['image_save'].get('reference_bound')),
                     'messages': [{k: m[k] for k in ('kind', 'status', 'claimed_at')} for m in messages],
                     'message_count': len(messages), 'max_messages': 2,
                     'next_message': operation, 'wait_seconds': round(wait, 1),
                     'save_readback_verified': bool(saved and saved['passed']),
                     'save_evidence': saved['evidence'] if saved else None,
                     'error': task['image_workflow'].get('error') or state.get('pause_reason'),
                     'remaining_seconds': state['remaining_seconds'],
                     'generation_completion_observable': False,
                     'message_acknowledgement_is_completion': False,
                     'polling_required': False, 'local_generation_job': False,
                     'next_actor': 'component_authored_host_turn' if not messages else 'host_image_or_save_turn',
                     'handoff_status': 'waiting_for_followup_turn' if not saved or not saved['passed'] else 'local_image_verified',
                     'handoff_note': 'This call has finished registering or reading the request. Image generation is not '
                                     'running on the Mac. The next operation belongs to a component-authored host message; '
                                     'repeated status calls in this response cannot execute that message or produce an image.',
                     'component_behavior': 'The live component posts the image brief once, then one local-save follow-up. '
                                           'No panel operation is needed. The host handles the messages and image generation.'}
            return CallToolResult(content=[TextContent(type='text', text=json.dumps(value, ensure_ascii=False))],
                                  structured_content=value, meta=reference.meta if reference else {})

    def host_observation(self, task_id):
        # Model-visible status queries indicate that the preceding host turn is
        # still doing work. Component polling must not keep resetting this clock.
        with self.harness.lock:
            task = self._task(task_id)
            if task['status'] == 'active' and task['image_workflow'].get('version', 1) >= 2:
                self._save(task)
            return self.result(task_id)

    def claim(self, task_id):
        with self.harness.lock:
            task = self._task(task_id)
            state = self.harness.get(task_id)
            kind, wait = self._next(task, state)
            if kind is None or wait > 0:
                return {'send': False, 'next_message': kind, 'wait_seconds': round(wait, 1)}
            actions = self.harness._actions(task_id)
            if any(a['status'] == 'running' for a in actions):
                return {'send': False, 'reason': 'Local action is still running.'}
            if kind == 'generate':
                prompt = task['image_save']['generation_prompt']
                if task['image_save'].get('reference_source'):
                    source = task['image_save']['reference_source']
                    reference_args = json.dumps({'workspace': task['workspace'], 'path': source['path']}, ensure_ascii=False)
                    prompt = ('请先调用 LocalPilot 的 read_image，参数为 ' + reference_args + '，在本轮查看实际原图。'
                              '然后使用 ChatGPT 原生图片编辑工具编辑刚读取的原图。'
                              '除明确要求修改的部分外，保持原图的构图、视角、物件位置、背景和已有文字。'
                              '编辑要求：' + prompt)
            elif kind == 'verify':
                prompt = (f'本次图片已经保存到 {task["image_save"]["path"]}，只剩任务验收。任务 ID：{task_id}。'
                          '请通过 LocalPilot get_task 读取当前计划和保存回执，读取本机成图核对画面。'
                          '如果满足原请求，用 update_plan 完成计划，再用 finish_task 提交画面核对说明和实际动作回执 ID；'
                          '如果不满足，请记录差异并保留未完成状态。本次续接只核对已有文件，不要求重新生成或保存。')
            else:
                binding = json.dumps(state['image_save_binding'], ensure_ascii=False)
                prompt = ('此前已请求原生生图，消息发送确认不代表已经产生图片。'
                          '如果本会话中已有这次请求对应的实际成图，请用 LocalPilot 的 save_chat_image 原生文件参数选择该成图，保存绑定为：' + binding + '。'
                          '工具会返回本机读回图片及回执；核对画面后完成计划，并用 finish_task 记录核对结果。'
                          '如果没有实际生成的图片，说明当前缺少成图并暂停这个任务即可；本次续接不要求重新生图。')
            token = uuid.uuid4().hex
            task['image_workflow']['messages'].append({'kind': kind, 'status': 'sending',
                                                      'token': token, 'claimed_at': time.time()})
            task['continuations'] += 1
            self._save(task)
            return {'send': True, 'task_id': task_id, 'kind': kind, 'token': token, 'prompt': prompt}

    def report(self, task_id, token, status, detail=''):
        if status not in ('sent', 'send_uncertain'):
            raise ToolError('图片流程消息状态无效。')
        with self.harness.lock:
            task = self._task(task_id)
            message = next((m for m in task['image_workflow']['messages'] if m['token'] == token), None)
            if message is None:
                raise ToolError('没有对应的图片流程消息。')
            if message['status'] == status:
                return self.result(task_id)
            if message['status'] != 'sending':
                raise ToolError('已经记录的消息结果不能改写。')
            message.update(status=status, acknowledged_at=time.time())
            if status == 'sent' and detail:
                try:
                    delivery = json.loads(detail)
                    if isinstance(delivery, dict):
                        safe = {k: delivery[k] for k in ('host_message_image', 'host_context_image', 'host_image_state_confirmed', 'image_id_scheme_preserved', 'upload_api_returned_scheme')
                                if isinstance(delivery.get(k), bool)}
                        if delivery.get('reference_transport') in ('none', 'message_image', 'message_image_probe', 'model_context_image', 'widget_image_ids', 'chatgpt_file_handoff'):
                            safe['reference_transport'] = delivery['reference_transport']
                        if re.fullmatch(r'[a-f0-9]{64}', str(delivery.get('reference_sha256', ''))):
                            safe['reference_sha256'] = delivery['reference_sha256']
                        message['delivery'] = safe
                except (ValueError, TypeError):
                    pass
            if status == 'send_uncertain':
                detail = re.sub(r'https?://\S+|(?:sediment://)?file[_-][A-Za-z0-9_-]+', '[reference]', str(detail))[:300]
                task['image_workflow']['error'] = '宿主没有确认图片流程消息，自动发送已停止。' + detail
                task['auto_continue'] = False
                if task['status'] == 'active':
                    task.update(status='paused', pause_reason='图片流程消息未取得宿主确认。')
            self.harness.storage.event('image_workflow_message', {'task_id': task_id, 'kind': message['kind'], 'status': status})
            self._save(task)
            return self.result(task_id)

    def saved_result(self, payload):
        """Add an audited disk read without losing an already committed write."""
        action, task = payload['action'], payload['task']
        if action['status'] != 'succeeded':
            payload['save_retry'] = task.get('image_save_retry', {'available': False})
            if payload['save_retry']['available']:
                payload['save_retry']['binding'] = task['image_save_binding']
                payload['save_retry']['operation'] = 'save_chat_image'
                payload['save_retry']['note'] = 'No file was written by this attempt. A new action ID preserves the failed receipt; select the same generated image with this task binding.'
            return self.harness.files.images.task_result(payload)
        read_id = action['action_id'] + ':readback'
        metadata = None
        try:
            if payload.get('replayed'):
                # A replayed write receipt describes the earlier commit. Read the
                # current disk again instead of presenting its old preview as current.
                metadata = self.harness.files.read_image(task['workspace'], task['image_save']['path'])
                payload['readback'] = {'status': 'verified' if metadata['sha256'] == action['result']['sha256'] else 'changed',
                                       'path': metadata['path'], 'sha256': metadata['sha256'], 'receipt_id': metadata['receipt_id']}
                payload['wrote_file'] = True
                return self._comparison_result(payload, metadata)
            read = self.harness.execute(task['task_id'], action['step_id'], 'read_image',
                        {'path': task['image_save']['path']}, read_id, observe=True)
            if read['action']['status'] == 'succeeded':
                metadata = read['action']['result']
                payload['readback'] = {'status': 'verified' if metadata['sha256'] == action['result']['sha256'] else 'changed',
                                       'path': metadata['path'], 'sha256': metadata['sha256'], 'action_id': read_id}
                payload['task'] = read['task']
        except (ToolError, OSError):
            pass
        payload.setdefault('readback', {'status': 'unavailable', 'path': task['image_save']['path']})
        payload['wrote_file'] = True
        payload['workflow_finalization'] = {'pending': True, 'task_id': task['task_id'],
            'next_step': 'After reviewing the returned disk image, complete the plan with update_plan and submit the visual assessment with finish_task.'}
        return self._comparison_result(payload, metadata)

    def _comparison_result(self, payload, metadata):
        result = self.harness.files.images.tool_result(payload, metadata, verification_image=True)
        source = payload['task'].get('image_save', {}).get('reference_source')
        if source and metadata is not None:
            try:
                original = self.handoff.reference_content(payload['task'])
                original.annotations = Annotations(audience=['assistant'])
                result.content[1:1] = [
                    TextContent(type='text', text='Original local reference snapshot for comparison:'), original,
                    TextContent(type='text', text='Actual saved output read from the Mac. Compare it with the original against the full request; byte verification alone does not prove visual correctness:')]
            except (ToolError, OSError):
                result.content.insert(1, TextContent(type='text', text='The original reference snapshot is unavailable for visual comparison. Read the source again before claiming the edit preserves its contents.'))
        return result
