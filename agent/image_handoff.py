"""Bind an immutable local reference image to a host-uploaded ChatGPT file."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import time
from mcp.types import CallToolResult, ImageContent, TextContent
from mcp.server.mcpserver.exceptions import ToolError


class ImageHandoff:
    def __init__(self, files, harness, editor):
        self.files, self.harness, self.editor = files, harness, editor

    def _task(self, task_id, *, active=False):
        task = self.harness._load(task_id)
        if not task.get('image_save', {}).get('reference_source'):
            raise ToolError('该任务没有本机原图传递请求。')
        if active:
            self.harness._active(task)
        return task

    def _save(self, task):
        task.update(revision=task['revision'] + 1, updated_at=time.time())
        self.harness._save(task)

    def reference_content(self, task):
        """An edit owns its frozen reference independently of the preview LRU."""
        with self.harness.lock:
            current = self._task(task['task_id'])
            source = current['image_save']['reference_source']
            attachment = current.get('reference_image_attachment')
            if attachment is None:
                try:
                    content = self.files.images.content(source)
                except ToolError:
                    # Legacy tasks may predate durable reference attachments.
                    # Reconstruct only if both original bytes and pixels match.
                    metadata = self.files.read_image(current['workspace'], source['path'], 1600, None, 0)
                    if metadata['sha256'] != source['sha256'] or metadata['image_sha256'] != source['image_sha256']:
                        raise ToolError('原参考快照已缺失且源图片发生变化，未替换任务绑定的原图。') from None
                    content = self.files.images.content(metadata)
                attachment = self.harness.result_images.save([{'data':content.data, 'mime_type':content.mime_type}])[0]
                current['reference_image_attachment'] = attachment
                # Internal attachment metadata is not task progress and must
                # not reset the host-turn idle clock or alter its request ID.
                self.harness._save(current)
            if attachment['sha256'] != source['image_sha256'] or attachment['bytes'] != source['image_bytes']:
                raise ToolError('任务原图附件与登记的像素校验值不一致。')
            image = self.harness.result_images.load([attachment])[0]
            return ImageContent(type='image', data=image['data'], mime_type=image['mime_type'])

    def preflight(self, workspace, output_path, request_id, output_expected_sha256=None, create_parents=False):
        with self.harness.storage.lock:
            registered = self.harness.storage.db.execute('SELECT 1 FROM image_save_requests WHERE request_id=?', (request_id,)).fetchone()
        if not registered:
            root, parts = self.files.path(workspace, output_path)
            if not parts:
                raise ToolError('请指定图片输出文件。')
            try:
                with self.files.directory(root, parts[:-1]) as directory:
                    try:
                        existing, _ = self.files.read_bytes(directory, parts[-1])
                    except FileNotFoundError:
                        existing = None
            except FileNotFoundError:
                if not create_parents:
                    raise ToolError('输出目录不存在；请确认保存位置或允许创建父目录。') from None
                existing = None
            except ToolError as exc:
                # Files.directory translates OS errors. A missing parent is
                # expected when creation was requested; other errors still fail.
                if create_parents and isinstance(exc.__cause__, FileNotFoundError):
                    existing = None
                else:
                    raise
            except OSError as exc:
                raise self.files.io_error(exc) from exc
            if existing is not None and hashlib.sha256(existing).hexdigest() != output_expected_sha256:
                raise ToolError('输出文件已存在；请使用新文件名，或提供允许覆盖的当前 SHA-256。尚未上传或生成图片。')
            if existing is None and output_expected_sha256 is not None:
                raise ToolError('需要覆盖的输出文件已经不存在，请重新确认保存位置。')

    def prepare(self, workspace, source_path, output_path, objective, request_id,
                expected_source_sha256=None, output_expected_sha256=None, create_parents=False, *, generation_prompt=None):
        self.preflight(workspace, output_path, request_id, output_expected_sha256, create_parents)
        metadata = self.files.read_image(workspace, source_path, 1600, None, 0)
        if expected_source_sha256 is not None and metadata['sha256'] != expected_source_sha256:
            raise ToolError('原图已经变化，请重新确认源图片的 SHA-256。')
        source = {k: metadata[k] for k in ('path', 'sha256', 'image_sha256', 'image_bytes', 'mime_type', 'width', 'height')}
        task = self.harness.prepare_image_save(workspace, output_path, objective, request_id,
                    output_expected_sha256, create_parents, reference_source=source, generation_prompt=generation_prompt)
        return self.result(task['task_id'])

    def result(self, task_id):
        with self.harness.lock:
            task = self._task(task_id)
            bound = task['image_save']
            bridge = task.get('reference_bridge', {})
            source = bound['reference_source']
            phase = bridge.get('phase', 'pending')
            state = self.harness.get(task_id)
            public = {'task_id': task_id, 'revision': state['revision'], 'observed_at': time.time(),
                      'source_status': phase, 'task_status': state['effective_status'],
                      'reference_bound': bound.get('reference_bound', False), 'source': source,
                      'objective': task['objective'], 'image_save_binding': state['image_save_binding'],
                      'component_handles_reference_transfer': True,
                      'save_tool': 'save_chat_image', 'save_tool_available_on_local_server': True,
                      'generation_reference_ready': bool(bound.get('reference_bound')),
                      'polling_required': False,
                      'next_action': {'actor': 'reference_component', 'trigger': 'component_rendered',
                                      'operation': 'upload_reference_and_post_followup'} if phase in ('pending','uploading') else None,
                      'handoff_summary': '本任务尚无已验证的原生编辑参考文件；此时生成可能另画一张图片，无法证明是在编辑本机原图。传递由组件启动，组件附图后会发起后续消息；反复查询 pending 不会启动上传。' if phase == 'pending' else None,
                      'followup_status': bridge.get('followup_status'), 'error': bridge.get('error'),
                      'followup_confirms_execution': False,
                      'error_detail': bridge.get('error_detail'),
                      'remaining_seconds': state['remaining_seconds'], 'idle_seconds': state['idle_seconds']}
            # Only the source component receives bytes and the host-issued file reference.
            private = {'task_id': task_id, 'file_name': Path(source['path']).stem + '-reference.' +
                       ('png' if source['mime_type'] == 'image/png' else 'jpg'),
                       'mime_type': source['mime_type'], 'sha256': source['image_sha256']}
            if phase in ('pending', 'uploading'):
                private['data'] = self.reference_content(task).data
            if bridge.get('file_id'):
                private['file_id'] = bridge['file_id']
            return CallToolResult(content=[TextContent(type='text', text=json.dumps(public, ensure_ascii=False))],
                                 structured_content=public, meta={'localpilot/referenceSource': private})

    def claim_upload(self, task_id, owner):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', owner):
            raise ToolError('上传实例标识无效。')
        with self.harness.lock:
            task = self._task(task_id, active=True)
            bridge = task.get('reference_bridge', {})
            if bridge.get('phase'):
                return {'claimed': False, 'phase': bridge['phase'], 'task_id': task_id}
            task['reference_bridge'] = {'phase': 'uploading', 'owner': owner, 'started_at': time.time()}
            self._save(task)
            return {'claimed': True, 'phase': 'uploading', 'task_id': task_id}

    def bind(self, task_id, owner, file):
        file = file.model_dump() if hasattr(file, 'model_dump') else file
        if not isinstance(file, dict) or not isinstance(file.get('file_id'), str) or not file['file_id']:
            raise ToolError('缺少宿主上传返回的文件参数。')
        with self.harness.lock:
            task = self._task(task_id, active=True)
            bridge = task.get('reference_bridge', {})
            if bridge.get('owner') != owner:
                raise ToolError('原图上传实例不匹配。')
            if bridge.get('phase') == 'ready':
                if bridge.get('file_id') != file['file_id']:
                    raise ToolError('已绑定的参考文件不能替换。')
                return self.result(task_id)
            if bridge.get('phase') != 'uploading':
                raise ToolError('原图传递已停止。')
            source = dict(task['image_save']['reference_source'])
        data, _, host = self.editor.fetch_bytes(file.get('download_url'))
        self.editor.decode(data)
        if hashlib.sha256(data).hexdigest() != source['image_sha256'] or len(data) != source['image_bytes']:
            raise ToolError('ChatGPT 参考文件与本机准备的原图字节不一致，拒绝绑定。')
        with self.harness.lock:
            task = self._task(task_id, active=True)
            bridge = task['reference_bridge']
            if bridge.get('owner') != owner or bridge.get('phase') not in ('uploading', 'ready'):
                raise ToolError('传递状态已改变，未绑定文件。')
            if bridge.get('phase') == 'ready':
                if bridge.get('file_id') != file['file_id']:
                    raise ToolError('已绑定的参考文件不能替换。')
                return self.result(task_id)
            receipt = self.files.storage.event('bind_image_edit_reference', {
                'task_id': task_id, 'source_path': source['path'], 'source_sha256': source['sha256'],
                'reference_sha256': source['image_sha256'], 'bytes': len(data), 'download_host': host})
            bridge.update(phase='ready', file_id=file['file_id'], receipt_id=receipt, bound_at=time.time())
            task['image_save']['reference_bound'] = True
            task['plan'] = [dict(p, status='completed' if p['id'] == 'reference' else
                                'in_progress' if p['id'] == 'save' else 'pending') for p in task['plan']]
            self._save(task)
            return self.result(task_id)

    def claim_followup(self, task_id):
        with self.harness.lock:
            task = self._task(task_id, active=True)
            if task.get('image_workflow'):
                return {'send': False, 'status': 'image_workflow_owns_messages'}
            bridge = task.get('reference_bridge', {})
            if bridge.get('phase') != 'ready' or bridge.get('followup_status'):
                return {'send': False, 'status': bridge.get('followup_status', 'reference_not_ready')}
            bridge.update(followup_status='sending', followup_at=time.time())
            self._save(task)
            return {'send': True, 'task_id': task_id,
                    'prompt': f'继续这项图片编辑请求：{task["objective"]}\n'
                              f'本机原图已作为参考图片附带，传递字节已校验。LocalPilot 任务：{task_id}。'}

    def report(self, task_id, status, detail=''):
        if status not in ('sent', 'unsupported', 'upload_failed', 'binding_failed', 'send_uncertain'):
            raise ToolError('传递结果无效。')
        with self.harness.lock:
            task = self._task(task_id)
            bridge = task.setdefault('reference_bridge', {})
            if status == 'sent':
                if bridge.get('phase') != 'ready' or bridge.get('followup_status') != 'sending':
                    raise ToolError('没有等待确认的后续消息。')
                bridge['followup_status'] = 'sent'
            else:
                reasons = {'unsupported': '当前宿主没有提供原图文件上传或图片状态接口。',
                           'upload_failed': '原图上传未完成，未继续生成。',
                           'binding_failed': '参考文件交接或字节校验失败，未继续生成。',
                           'send_uncertain': '参考图已绑定，但后续消息未取得确认，未重复发送。'}
                message = reasons[status]
                if status == 'binding_failed' and task['image_save'].get('reference_bound'):
                    message = '原图字节已校验，但组件后续交接未完成。'
                detail = re.sub(r'https?://\S+', '[url]', str(detail))
                detail = re.sub(r'(?:sediment://)?file[_-][A-Za-z0-9_-]+', '[file]', detail)[:400]
                bridge.update(phase='failed', error=message, error_detail=detail)
                self.files.storage.event('image_edit_handoff_error', {'task_id': task_id, 'stage': status,
                    'reference_bound': task['image_save'].get('reference_bound', False), 'detail': detail})
                task['auto_continue'] = False
                if task['status'] == 'active':
                    task.update(status='paused', pause_reason=reasons[status])
            self._save(task)
            return self.result(task_id)
