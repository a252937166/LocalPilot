"""Local pixel edits and bounded imports of ChatGPT-authorized image files."""
from __future__ import annotations
import hashlib
from contextlib import contextmanager, nullcontext
import io
import math
from pathlib import Path
import ssl
import time
from urllib.parse import urlsplit
import urllib.request
import urllib.error
import warnings
import certifi
from PIL import Image, ImageEnhance, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field
from mcp.server.mcpserver.exceptions import ToolError
from images import FORMATS, MAX_PIXELS


class ChatImageFile(BaseModel):
    """Exact schema required by openai/fileParams; signed URLs are never put in receipts."""
    model_config = ConfigDict(extra='forbid')
    download_url: str = Field(description='HTTPS download URL supplied by the ChatGPT native file input.')
    file_id: str = Field(description='File identifier supplied by ChatGPT for the selected image.')
    mime_type: str = Field(default='', description='Optional image MIME type supplied by the host, for example image/png.')
    file_name: str = Field(default='', description='Optional original filename supplied by the host.')


class ImageFileInputError(ToolError):
    """Safe diagnostics: never retain the supplied URL, path, file ID or signed query."""
    def __init__(self, code, message, *, stage='file_input', input_kind='invalid_url'):
        super().__init__(message)
        self.receipt = {'error':message, 'error_code':code, 'error_source':'localpilot',
                        'error_stage':stage, 'input_kind':input_kind, 'wrote_file':False}


def validate_file_url(url, *, stage='file_input'):
    def reject(code, message, kind='invalid_url'):
        raise ImageFileInputError(code, message, stage=stage, input_kind=kind)
    if not isinstance(url,str) or not url.strip():
        reject('MISSING_DOWNLOAD_URL', '没有收到图片下载地址，需要真实的 HTTPS 图片链接；图片尚未保存。', 'empty')
    if url.startswith(('/', '~', 'sandbox:', 'file:', 'attachment:')):
        reject('FILE_REFERENCE_NOT_DOWNLOAD_URL', '收到的是文件路径或内部引用，不是可下载的 HTTPS 地址。请由 ChatGPT 原生文件参数完成交接；图片尚未保存。', 'file_path_or_reference')
    if url.startswith(('file_', 'file-', 'image_', 'image-')):
        reject('FILE_REFERENCE_NOT_DOWNLOAD_URL', '收到的是文件标识，不是下载地址。file_id 不能替代 download_url；图片尚未保存。', 'file_id')
    if url.startswith('data:'):
        reject('INLINE_IMAGE_NOT_DOWNLOAD_URL', '收到的是内嵌图片数据，不是下载地址。此接口需要 ChatGPT 原生文件参数；图片尚未保存。', 'inline_data')
    if any(ord(c)<=32 or ord(c)==127 for c in url):
        reject('MALFORMED_DOWNLOAD_URL', '收到的下载地址含有空白或控制字符，无法使用；图片尚未保存。')
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        parsed.port  # Validate syntax/range; explicit HTTPS ports are supported.
    except ValueError:
        reject('MALFORMED_DOWNLOAD_URL', '收到的下载地址格式无效；图片尚未保存。')
    if not parsed.scheme or not host:
        reject('MALFORMED_DOWNLOAD_URL', '收到的内容不是完整的下载地址，需要包含 HTTPS 协议和域名；图片尚未保存。')
    if parsed.scheme != 'https':
        reject('DOWNLOAD_URL_REQUIRES_HTTPS', '收到的下载地址未使用 HTTPS；图片尚未保存。', 'non_https_url')
    if parsed.username is not None or parsed.password is not None:
        reject('DOWNLOAD_URL_HAS_CREDENTIALS', '下载地址不应包含用户名或密码；图片尚未保存。')
    return host


class FileRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_file_url(newurl, stage='download_redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ImageEditor:
    def __init__(self, files):
        self.files = files

    @staticmethod
    def _checkpoint(control, deadline):
        expired = deadline is not None and time.time() >= deadline
        if expired or (control is not None and control.requested.is_set()):
            if control is not None:
                control.requested.set()
                control.stop_confirmed = not getattr(control, 'image_committed', False)
            error = ImageFileInputError('IMAGE_TIMEOUT' if expired else 'IMAGE_CANCELLED',
                '图片操作已超过任务期限；未写入文件。' if expired else '图片操作已停止；未写入文件。', stage='before_write')
            if control is not None:
                error.receipt.update(control.summary())
            raise error

    @contextmanager
    def _operation(self, control, deadline):
        try:
            self._checkpoint(control, deadline)
            yield
        finally:
            if control is not None:
                control.settled.set()

    def _write(self, *args, _control=None, _deadline=None, **kwargs):
        # Linearize pause against the atomic write. Once pause is acknowledged,
        # a downloaded/decoded image cannot subsequently commit to disk.
        with _control.lock if _control is not None else nullcontext():
            self._checkpoint(_control, _deadline)
            result = self.files.write_bytes(*args, **kwargs)
            if _control is not None:
                _control.image_committed = True
            return result

    @staticmethod
    def decode(data, frame_index=None):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data), formats=list(FORMATS)) as source:
                    frames = getattr(source,'n_frames',1)
                    if frames > 1 and frame_index is None:
                        raise ToolError('多帧图片请明确指定 frame_index；编辑结果会保存为单帧图片。')
                    index = frame_index if frame_index is not None else 0
                    if type(index) is not int or not 0 <= index < min(frames,500):
                        raise ToolError('frame_index 超出图片帧范围。')
                    source.seek(index)
                    if source.width * source.height > MAX_PIXELS:
                        raise ToolError('原图超过 4000 万像素。')
                    source_format = source.format
                    normalized = ImageOps.exif_transpose(source)
                    mode = 'RGBA' if normalized.mode in ('RGBA','LA') or 'transparency' in normalized.info else 'RGB'
                    clean = Image.new(mode, normalized.size)
                    clean.paste(normalized.convert(mode))
                    return clean, source_format
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ToolError('图片像素过大，拒绝解码。') from exc
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError) as exc:
            raise ToolError('图片损坏或格式不支持。') from exc

    def encode(self, image, path):
        fmt = {'.png':'PNG','.jpg':'JPEG','.jpeg':'JPEG','.webp':'WEBP'}.get(Path(path).suffix.lower())
        if fmt is None:
            raise ToolError('输出文件扩展名必须为 .png、.jpg、.jpeg 或 .webp。')
        if fmt == 'JPEG' and image.mode == 'RGBA':
            background = Image.new('RGB',image.size,'white');background.paste(image,mask=image.getchannel('A'));image=background
        buffer=io.BytesIO()
        options = {'quality':95} if fmt in ('JPEG','WEBP') else {}
        image.save(buffer,format=fmt,**options)
        data=buffer.getvalue()
        if len(data)>self.files.maximum:
            raise ToolError('编辑后的图片超过文件大小上限；请缩小尺寸或保存为 JPEG/WebP。')
        return data,fmt

    def edit(self, workspace, path, output_path, expected_sha256, output_expected_sha256=None,
             crop=None, resize=None, rotate=0, flip='none', brightness=1.0, contrast=1.0, saturation=1.0,
             frame_index=None, *, _control=None, _deadline=None):
        with self._operation(_control, _deadline):
            return self._edit(workspace,path,output_path,expected_sha256,output_expected_sha256,
                              crop,resize,rotate,flip,brightness,contrast,saturation,frame_index,
                              _control=_control,_deadline=_deadline)

    def _edit(self, workspace, path, output_path, expected_sha256, output_expected_sha256,
              crop, resize, rotate, flip, brightness, contrast, saturation, frame_index, *, _control, _deadline):
        root,parts=self.files.path(workspace,path)
        output_root,output_parts=self.files.path(workspace,output_path)
        if not parts or not output_parts:raise ToolError('请指定源图片和输出文件路径。')
        if type(rotate) is not int or rotate not in (0,90,180,270):raise ToolError('rotate 只接受顺时针 0、90、180、270 度。')
        if flip not in ('none','horizontal','vertical'):raise ToolError('flip 只接受 none、horizontal、vertical。')
        for name,value,low in [('brightness',brightness,.1),('contrast',contrast,.1),('saturation',saturation,0)]:
            if type(value) not in (int,float) or not math.isfinite(value) or not low<=value<=3:
                raise ToolError(f'{name} 必须是 {low}–3 的有限数值。')
        if resize is not None and (not isinstance(resize,list) or len(resize)!=2 or any(type(v) is not int or not 1<=v<=12000 for v in resize) or resize[0]*resize[1]>MAX_PIXELS):
            raise ToolError('resize 为 [width,height]，边长 1–12000 且总像素不超过 4000 万。')
        if crop is not None and (not isinstance(crop,list) or len(crop)!=4 or any(type(v) is not int for v in crop)):
            raise ToolError('crop 为四个整数 [left,top,right,bottom]。')
        # Serialize our own writers across read/transform/commit; external changes are still checked at commit.
        with self.files.lock:
            try:
                with self.files.directory(root,parts[:-1]) as fd:data,_=self.files.read_bytes(fd,parts[-1])
            except OSError as exc:raise self.files.io_error(exc) from exc
            source_sha=hashlib.sha256(data).hexdigest()
            if source_sha!=expected_sha256:raise ToolError('源图片已变化；请重新 read_image 并使用其 sha256。')
            image,source_format=self.decode(data,frame_index)
            original_size=list(image.size)
            if crop is not None:
                l,t,r,b=crop
                if not 0<=l<r<=image.width or not 0<=t<b<=image.height:raise ToolError('crop 超出旋转校正后的原图范围，或区域为空。')
                image=image.crop(tuple(crop))
            if rotate:image=image.rotate(-rotate,expand=True)
            if flip=='horizontal':image=ImageOps.mirror(image)
            elif flip=='vertical':image=ImageOps.flip(image)
            if resize is not None:image=image.resize(tuple(resize),Image.Resampling.LANCZOS)
            for enhancer,factor in [(ImageEnhance.Brightness,brightness),(ImageEnhance.Contrast,contrast),(ImageEnhance.Color,saturation)]:
                if factor!=1:image=enhancer(image).enhance(factor)
            encoded,fmt=self.encode(image,output_path)
            same=root.joinpath(*parts)==output_root.joinpath(*output_parts)
            if same and output_expected_sha256 not in (None,source_sha):raise ToolError('原位编辑的输出校验值必须与源图片一致。')
            result=self._write(workspace,output_path,encoded,source_sha if same else output_expected_sha256,tool='edit_image',
                               _control=_control,_deadline=_deadline)
        result.update(source_path=str(root.joinpath(*parts)),source_sha256=source_sha,source_format=source_format,
                      original_size=original_size,width=image.width,height=image.height,output_format=fmt,
                      edits={'crop':crop,'rotate_clockwise':rotate,'flip':flip,'resize':resize,'brightness':brightness,'contrast':contrast,'saturation':saturation},
                      note='Pixel transforms only; no generative model call. EXIF orientation corrected and source metadata removed.')
        return result

    def save(self, workspace, path, file, expected_sha256=None, create_parents=False, *, _control=None, _deadline=None):
        with self._operation(_control, _deadline):
            return self._save(workspace,path,file,expected_sha256,create_parents,_control=_control,_deadline=_deadline)

    def _save(self, workspace, path, file, expected_sha256, create_parents, *, _control, _deadline):
        # Check destination scope before any network request. Never use signed URL parameters as audit data.
        self.files.path(workspace,path)
        if isinstance(file,ChatImageFile):file=file.model_dump()
        if not isinstance(file,dict) or not isinstance(file.get('download_url'),str) or not isinstance(file.get('file_id'),str) or not file['file_id']:
            raise ImageFileInputError('MISSING_FILE_PARAMETER', '没有收到完整的 ChatGPT 文件参数，需要 download_url 和 file_id；图片尚未保存。', input_kind='incomplete_file')
        result=self._download(workspace,path,file['download_url'],expected_sha256,create_parents,'save_chat_image',
                              _control=_control,_deadline=_deadline)
        result['file_id']=file['file_id']
        return result

    def download(self, workspace, path, url, expected_sha256=None, create_parents=False, *, _control=None, _deadline=None):
        """Download a real user-provided URL without requiring a ChatGPT file identifier."""
        with self._operation(_control, _deadline):
            return self._download(workspace,path,url,expected_sha256,create_parents,'download_image',
                                  _control=_control,_deadline=_deadline)

    def fetch_bytes(self, url, *, _control=None, _deadline=None):
        """Fetch a bounded HTTPS image payload without changing a local file."""
        self._checkpoint(_control, _deadline)
        host=validate_file_url(url)
        # Keep system/explicit trust and supplement Python installs that ship without a CA bundle.
        # Never disable hostname or certificate verification, including on redirect targets.
        tls=ssl.create_default_context()
        tls.load_verify_locations(cafile=certifi.where())
        opener=urllib.request.build_opener(FileRedirects(),urllib.request.HTTPSHandler(context=tls))
        started=time.monotonic()
        try:
            request=urllib.request.Request(url,headers={'Accept':'image/*'})
            with opener.open(request,timeout=20) as response:
                final_host=validate_file_url(response.geturl(), stage='download_redirect')
                length=response.headers.get('Content-Length')
                if length and int(length)>self.files.maximum:raise ToolError('下载的图片超过本机文件大小上限。')
                chunks=[];total=0
                while True:
                    self._checkpoint(_control, _deadline)
                    if time.monotonic()-started>40:
                        raise ImageFileInputError('IMAGE_DOWNLOAD_TIMEOUT', '下载图片超时；尚未写入文件。', stage='download', input_kind='https_url')
                    chunk=response.read(min(65536,self.files.maximum+1-total))
                    if not chunk:break
                    chunks.append(chunk);total+=len(chunk)
                    if total>self.files.maximum:raise ToolError('下载的图片超过本机文件大小上限。')
                data=b''.join(chunks)
        except ToolError:raise
        except urllib.error.HTTPError as exc:
            code = 'IMAGE_DOWNLOAD_TEMPORARY' if exc.code in (408, 500, 502, 503, 504) else 'IMAGE_DOWNLOAD_REJECTED'
            raise ImageFileInputError(code, f'图片服务器返回 HTTP {exc.code}；尚未写入文件。', stage='download', input_kind='https_url') from None
        except (TimeoutError, ConnectionError):
            raise ImageFileInputError('IMAGE_DOWNLOAD_TIMEOUT', '图片下载连接中断或超时；尚未写入文件。', stage='download', input_kind='https_url') from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, ConnectionError)):
                raise ImageFileInputError('IMAGE_DOWNLOAD_TIMEOUT', '图片下载连接中断或超时；尚未写入文件。', stage='download', input_kind='https_url') from None
            raise ToolError('无法下载图片；链接可能失效、证书无法验证或网络不可用。尚未写入文件。') from None
        except Exception as exc:
            # urllib exceptions include signed URLs. Do not return/log those URLs.
            raise ToolError('无法下载图片；链接可能失效、证书无法验证或网络不可用。尚未写入文件。') from None
        return data, host, final_host

    def _download(self, workspace, path, url, expected_sha256, create_parents, tool, *, _control=None, _deadline=None):
        self.files.path(workspace,path)
        kwargs = {'_control':_control, '_deadline':_deadline} if _control is not None or _deadline is not None else {}
        data,host,final_host=self.fetch_bytes(url,**kwargs)
        self._checkpoint(_control, _deadline)
        image,source_format=self.decode(data)
        fmt = {'.png':'PNG','.jpg':'JPEG','.jpeg':'JPEG','.webp':'WEBP'}.get(Path(path).suffix.lower())
        if fmt is None:
            raise ToolError('输出文件扩展名必须为 .png、.jpg、.jpeg 或 .webp。')
        preserved = source_format == fmt
        encoded = data if preserved else self.encode(image,path)[0]
        result=self._write(workspace,path,encoded,expected_sha256,create_parents,tool=tool,_control=_control,_deadline=_deadline)
        result.update(download_host=final_host,requested_host=host,source_sha256=hashlib.sha256(data).hexdigest(),source_format=source_format,
                      width=image.width,height=image.height,output_format=fmt,source_bytes_preserved=preserved,
                      note='Saved the downloaded image after image validation; no model API call. Same-format files preserve the original bytes; format conversion strips metadata.')
        return result
