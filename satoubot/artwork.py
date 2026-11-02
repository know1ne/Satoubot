import io
import warnings
from PIL import Image, ImageOps
from .discovery import fetch_bytes


async def landscape(url, small=False):
    data = await fetch_bytes(url)
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as source:
            source = ImageOps.exif_transpose(source).convert('RGB')
            size = (320, 180) if small else (960, 540)
            if source.width >= source.height:
                image = ImageOps.fit(source, size)
            else:
                image = Image.new('RGB', size, '#151515')
                source.thumbnail(size)
                image.paste(source, ((size[0]-source.width)//2, (size[1]-source.height)//2))
            out = io.BytesIO()
            for quality in (85, 65, 45, 25):
                out.seek(0); out.truncate()
                image.save(out, 'JPEG', quality=quality, optimize=True)
                if not small or out.tell() < 20_000: break
            out.seek(0)
            return out
