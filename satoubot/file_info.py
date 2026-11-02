import re
from email.message import Message
from urllib.parse import unquote, urlparse
import httpx
from .transfer import safe_name


async def probe(url, headers=None):
    if urlparse(url).scheme not in ('http','https'): raise ValueError('Unsupported URL')
    async with httpx.AsyncClient(follow_redirects=True,timeout=15) as client:
        response=await client.head(url,headers={**(headers or {}),'Accept-Encoding':'identity'})
        response.raise_for_status()
        length=response.headers.get('content-length','')
        size=int(length) if length.isdigit() and int(length)>0 else None
        message=Message();message['content-disposition']=response.headers.get('content-disposition','')
        name=message.get_filename() or unquote(urlparse(str(response.url)).path.rsplit('/',1)[-1])
        return {'size':size,'filename':safe_name(name or 'media'),'content_type':response.headers.get('content-type','')}


def clean_title(movie, stream):
    title=str(movie.get('name','Movie'))
    year=str(movie.get('year') or movie.get('releaseInfo') or '')[:4]
    details=' '.join(str(stream.get(k) or '') for k in ('quality','title','name','filename','language','description'))
    details=re.sub(r'[._-]+',' ',details)
    quality=re.search(r'(?i)\b(2160|1080|720|480|360)p\b|\b4k\b',details)
    languages=[]
    for pattern,label in [(r'\b(hindi|hin)\b','Hindi'),(r'\b(english|eng)\b','English'),(r'\b(tamil|tam)\b','Tamil'),(r'\b(telugu|tel)\b','Telugu'),(r'\b(malayalam|mal)\b','Malayalam')]:
        if re.search(pattern,details,re.I):languages.append(label)
    codec=re.search(r'\b(HEVC|x265|x264|H264|H265|AVC)\b',details,re.I)
    text=' '.join([title,year,quality.group(0).upper() if quality else '',codec.group(0).upper() if codec else '',*languages])
    return safe_name(re.sub(r'[._]+',' ',text)).strip()
