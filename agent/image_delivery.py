"""Validated, size-bounded pixels for browser and bridged MCP image results."""
import base64
import io
import warnings

from PIL import Image, ImageOps

MAX_IMAGE_BYTES = 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_PIXELS = 40_000_000
MIMES = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp', 'GIF': 'image/gif'}


def encode_preview(picture, max_side=4096, max_bytes=MAX_IMAGE_BYTES):
    """Return encoded bytes, their actual MIME and dimensions; enforce the byte limit even for noise."""
    picture = picture.copy()
    picture.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    picture.save(buffer, format='PNG', optimize=True)
    encoded = buffer.getvalue()
    if len(encoded) <= max_bytes:
        return encoded, 'image/png', picture.size
    if picture.mode == 'RGBA':
        background = Image.new('RGB', picture.size, 'white')
        background.paste(picture, mask=picture.getchannel('A'))
        picture = background
    else:
        picture = picture.convert('RGB')
    while True:
        buffer = io.BytesIO()
        picture.save(buffer, format='JPEG', quality=82, optimize=True)
        encoded = buffer.getvalue()
        if len(encoded) <= max_bytes:
            return encoded, 'image/jpeg', picture.size
        picture.thumbnail((max(1, int(picture.width * .75)), max(1, int(picture.height * .75))), Image.Resampling.LANCZOS)


def validated_preview(data_b64, max_side=1600):
    if not isinstance(data_b64, str) or len(data_b64) > 4 * ((MAX_SOURCE_BYTES + 2) // 3):
        raise ValueError('image exceeds the 16 MiB input limit')
    raw = base64.b64decode(data_b64, validate=True)
    if not raw or len(raw) > MAX_SOURCE_BYTES:
        raise ValueError('empty or oversized image')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw), formats=list(MIMES)) as source:
            if source.width * source.height > MAX_SOURCE_PIXELS:
                raise ValueError('image exceeds the 40 million pixel limit')
            source.load()  # A valid header alone does not prove the pixels can be decoded.
            mime = MIMES[source.format]
            oriented = ImageOps.exif_transpose(source)
            if len(raw) <= MAX_IMAGE_BYTES and max(source.size) <= max_side and not source.getexif().get(274):
                return data_b64, mime, len(raw), source.size
            encoded, mime, dims = encode_preview(oriented.convert('RGBA' if 'A' in oriented.getbands() else 'RGB'), max_side)
            return base64.b64encode(encoded).decode('ascii'), mime, len(encoded), dims
