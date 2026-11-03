"""Download finite, unencrypted HLS playlists using public-only HTTP requests."""
import os
import re
import time
from urllib.parse import urljoin, urlsplit

import httpx
from .public_transport import PublicTransport
from .transfer import MAX_DOWNLOAD_BYTES

MAX_HLS_BYTES = MAX_DOWNLOAD_BYTES
MAX_PLAYLIST_BYTES = 1_000_000
MAX_SEGMENTS = 20_000


class HLSError(ValueError):
    pass


def origin(url):
    parts = urlsplit(url)
    return (parts.scheme.lower(), (parts.hostname or '').lower(), parts.port or (443 if parts.scheme == 'https' else 80))


def headers_for(url, initial, headers):
    # Credentials supplied for one origin must never be sent to another origin.
    blocked = {'host', 'range', 'accept-encoding', 'connection', 'content-length', 'transfer-encoding', 'proxy-authorization'}
    if origin(url) != origin(initial): blocked |= {'authorization', 'cookie'}
    return {**{str(k): str(v) for k, v in (headers or {}).items() if str(k).lower() not in blocked}, 'Accept-Encoding': 'identity'}


def attributes(line):
    return {m.group(1): (m.group(2) if m.group(2) is not None else m.group(3))
            for m in re.finditer(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))(?:,|$)', line)}


async def playlist(client, url, initial, headers):
    async with client.stream('GET', url, headers=headers_for(url, initial, headers)) as response:
        if response.status_code in (401, 403): raise HLSError('Source requires login or blocks access')
        if response.status_code in (429, 503): raise HLSError('Source is temporarily unavailable; retry later')
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=8192):
            data.extend(chunk)
            if len(data) > MAX_PLAYLIST_BYTES: raise HLSError('Playlist is too large')
        try: text = bytes(data).decode('utf-8-sig')
        except UnicodeError: raise HLSError('Invalid HLS playlist') from None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines or lines[0] != '#EXTM3U': raise HLSError('Source is not an HLS playlist')
        return lines, str(response.url)


async def inspect_hls(url, headers=None, quality=None):
    async with httpx.AsyncClient(transport=PublicTransport(), follow_redirects=True, max_redirects=5,
                                 timeout=httpx.Timeout(30, connect=15), trust_env=False) as client:
        return await plan_hls(client, url, headers, quality)


async def plan_hls(client, url, headers=None, quality=None):
    initial = url
    seen = set()
    for _ in range(4):
        if url in seen: raise HLSError('Playlist loop detected')
        seen.add(url)
        lines, base = await playlist(client, url, initial, headers)
        if any(line.startswith(('#EXT-X-SESSION-KEY', '#EXT-X-I-FRAMES-ONLY')) for line in lines):
            raise HLSError('Encrypted or unsupported playlist')
        if any(line.startswith('#EXT-X-MEDIA:') and 'URI=' in line for line in lines):
            raise HLSError('Separate audio or subtitle playlists are not supported yet')
        variants = []
        for i, line in enumerate(lines):
            if line.startswith('#EXT-X-STREAM-INF:'):
                if i + 1 >= len(lines) or lines[i+1].startswith('#'): raise HLSError('Invalid HLS master playlist')
                attrs = attributes(line.split(':', 1)[1])
                height = re.search(r'\d+x(\d+)', attrs.get('RESOLUTION', ''))
                bandwidth = attrs.get('BANDWIDTH', '0')
                variants.append((int(height.group(1)) if height else 0, int(bandwidth) if bandwidth.isdigit() else 0, urljoin(base, lines[i+1])))
        if variants:
            match = re.search(r'(\d{3,4})\s*p?', str(quality or ''), re.I)
            target = int(match.group(1)) if match else None
            known = [v for v in variants if v[0]]
            choices = [v for v in known if v[0] <= target] if target else known
            if target and known and not choices: selected = min(known, key=lambda v: (v[0], v[1]))
            else: selected = max(choices or variants, key=lambda v: (v[0], v[1]))
            url = selected[2]
            continue
        if '#EXT-X-ENDLIST' not in lines: raise HLSError('Live HLS streams are not supported')
        if any(line.startswith(('#EXT-X-BYTERANGE', '#EXT-X-DISCONTINUITY', '#EXT-X-GAP', '#EXT-X-PART')) for line in lines):
            raise HLSError('This HLS playlist uses unsupported segment features')
        maps = [line for line in lines if line.startswith('#EXT-X-MAP:')]
        if len(maps) > 1: raise HLSError('Multiple initialization segments are not supported')
        for line in lines:
            if line.startswith('#EXT-X-KEY:') and attributes(line.split(':',1)[1]).get('METHOD') != 'NONE':
                raise HLSError('Encrypted HLS is not supported')
        segments = [urljoin(base, line) for line in lines if not line.startswith('#')]
        if not segments or len(segments) > MAX_SEGMENTS: raise HLSError('Playlist has no segments or too many segments')
        if maps:
            attrs = attributes(maps[0].split(':',1)[1])
            if not attrs.get('URI') or attrs.get('BYTERANGE'): raise HLSError('Unsupported HLS initialization segment')
            segments.insert(0, urljoin(base, attrs['URI']))
        return {'urls': segments, 'format': 'mp4' if maps else 'ts', 'initial': initial}
    raise HLSError('Too many nested playlists')


async def download_hls(url, destination, expected_format, progress, headers=None, quality=None, limit=MAX_HLS_BYTES):
    temporary = destination.with_suffix(destination.suffix + '.part')
    total = 0
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(transport=PublicTransport(), follow_redirects=True, max_redirects=5,
                                     timeout=httpx.Timeout(60, connect=20), trust_env=False) as client:
            plan = await plan_hls(client, url, headers, quality)
            if plan['format'] != expected_format: raise HLSError('Playlist format changed; select it again')
            with temporary.open('wb') as output:
                for index, segment in enumerate(plan['urls']):
                    async with client.stream('GET', segment, headers=headers_for(segment, url, headers)) as response:
                        if response.status_code in (401,403): raise HLSError('A segment requires login or blocks access')
                        if response.status_code in (429,503): raise HLSError('Source is temporarily unavailable; retry later')
                        response.raise_for_status()
                        count = 0
                        async for chunk in response.aiter_bytes(chunk_size=256*1024):
                            if total + len(chunk) > limit: raise HLSError('HLS download exceeds the 10 GB local limit')
                            if count == 0:
                                allowed = (b'ftyp',) if index == 0 else (b'styp', b'moof', b'sidx', b'emsg', b'prft', b'free')
                                if expected_format == 'mp4' and (len(chunk) < 8 or chunk[4:8] not in allowed): raise HLSError('Invalid HLS MP4 segment')
                                if expected_format == 'ts' and (not chunk or chunk[0] != 0x47): raise HLSError('Unsupported HLS segment format')
                            output.write(chunk)
                            count += len(chunk); total += len(chunk)
                            await progress(total, None, total/max(time.monotonic()-started, .001))
                        length = response.headers.get('content-length')
                        if not count or (length and length.isdigit() and int(length) != count): raise HLSError('HLS segment is incomplete')
        if not total: raise HLSError('HLS download is empty')
        os.replace(temporary, destination)
        return total
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
