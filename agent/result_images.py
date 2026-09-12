"""Durable, content-addressed images for task receipts and idempotent replay."""
import base64
import binascii
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

from mcp.server.mcpserver.exceptions import ToolError


class ResultImages:
    # Separate from the evictable read_image preview cache: a saved action owns
    # its original pixels for as long as the local task history is retained.
    MAX_BYTES = 16 * 1024 * 1024

    def __init__(self, state_dir):
        self.directory = Path(state_dir) / 'task-images'

    def save(self, images):
        refs = []
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for image in images:
            try:
                raw = base64.b64decode(image['data'], validate=True)
                mime = image['mime_type']
                if not raw or len(raw) > self.MAX_BYTES or mime not in ('image/png', 'image/jpeg', 'image/webp', 'image/gif'):
                    raise ValueError('invalid image attachment')
            except (KeyError, ValueError, TypeError, binascii.Error) as exc:
                raise ToolError('图片回执附件无效或超过 16 MiB，未保存。') from exc
            digest = hashlib.sha256(raw).hexdigest()
            # Atomic publication avoids a replay seeing a partially written image.
            fd, temporary = tempfile.mkstemp(dir=self.directory, prefix='.image-')
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                os.replace(temporary, self.directory / digest)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            refs.append({'sha256': digest, 'mime_type': mime, 'bytes': len(raw)})
        return refs

    def load(self, refs):
        images = []
        for ref in refs:
            try:
                digest = ref['sha256']
                if not re.fullmatch(r'[0-9a-f]{64}', digest):
                    raise ValueError('invalid hash')
                fd = os.open(self.directory / digest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= self.MAX_BYTES:
                        raise ValueError('invalid attachment')
                    raw = stream.read(self.MAX_BYTES + 1)
                if len(raw) != ref['bytes'] or hashlib.sha256(raw).hexdigest() != digest:
                    raise ValueError('attachment changed')
                images.append({'data': base64.b64encode(raw).decode('ascii'), 'mime_type': ref['mime_type']})
            except (OSError, KeyError, ValueError, TypeError) as exc:
                raise ToolError('原动作的图片附件缺失或校验失败，无法重放原画面；原动作没有重新执行。') from exc
        return images
