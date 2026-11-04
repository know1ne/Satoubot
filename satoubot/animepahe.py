"""Adapter for a separately hosted AnimePahe API; no upstream code is executed here."""
import asyncio
import copy
import ipaddress
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx
from .public_transport import PublicTransport

DEFAULT_API = 'http://127.0.0.1:3000/'
NAME = 'AnimePahe (API)'
_cooldowns = {}
_request_locks = {}
_next_requests = {}
_response_cache = {}
REQUEST_INTERVAL = 3.0
DEFAULT_COOLDOWN = 300.0
CACHE_SECONDS = 60.0
MAX_CACHE_ENTRIES = 16
PLAY_TIMEOUT = 75.0


class RateLimitError(ValueError):
    """The API asked us to stop sending requests temporarily."""



def normalize_base(value):
    value = value.strip().rstrip('/') + '/'
    url = httpx.URL(value)
    if url.scheme not in ('http', 'https') or not url.host or url.userinfo or url.query or url.fragment:
        raise ValueError('ANIMEPAHE_API_URL must be an HTTP(S) base URL without credentials, query or fragment')
    if url.host.lower() == 'localhost':
        url = url.copy_with(host='127.0.0.1')
    return str(url)


def register_default(store, base=DEFAULT_API):
    base = normalize_base(base)
    existing = store.db.execute('SELECT kind FROM manifests WHERE url=?', (base,)).fetchone()
    if existing and existing['kind'] != 'animepahe':
        raise ValueError('AnimePahe API URL conflicts with an existing add-on')
    previous = store.setting('animepahe_default_base')
    if previous and previous != base:
        row = store.db.execute("SELECT kind FROM manifests WHERE url=?", (previous,)).fetchone()
        if row and row['kind'] == 'animepahe':
            import hashlib
            old_key = 'enabled:' + hashlib.sha256(previous.encode()).hexdigest()
            new_key = 'enabled:' + hashlib.sha256(base.encode()).hexdigest()
            enabled = store.setting(old_key, '1')
            store.db.execute('INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)', (new_key, enabled))
            store.db.execute('DELETE FROM manifests WHERE url=? AND kind=?', (previous, 'animepahe'))
    store.db.execute('INSERT OR IGNORE INTO manifests(url,kind,data) VALUES (?,?,?)',
                     (base, 'animepahe', json.dumps({'name':NAME, 'catalogs':[]})))
    store.db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)', ('animepahe_default_base', base))
    store.db.commit()
    return base


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,128}', value):
        raise ValueError('Invalid AnimePahe identifier')
    return value


def _loopback(host):
    try: return ipaddress.ip_address(host).is_loopback
    except ValueError: return False


async def request(base, path, params=None):
    # One request per API at a time, with spacing across searches, releases and play.
    lock = _request_locks.setdefault(base, asyncio.Lock())
    async with lock:
        if _cooldowns.get(base, 0) > time.monotonic():
            raise RateLimitError('AnimePahe API requested a wait before retrying')
        cacheable = path == 'search' or path.endswith('/releases')
        cache_key = (base, path, tuple(sorted((str(k), str(v)) for k, v in (params or {}).items())))
        cached = _response_cache.get(cache_key) if cacheable else None
        if cached and cached[0] > time.monotonic():
            return copy.deepcopy(cached[1])
        delay = _next_requests.get(base, 0) - time.monotonic()
        if delay > 0: await asyncio.sleep(delay)
        if _cooldowns.get(base, 0) > time.monotonic():
            raise RateLimitError('AnimePahe API requested a wait before retrying')
        _next_requests[base] = time.monotonic() + REQUEST_INTERVAL
        transport = None if _loopback(httpx.URL(base).host) else PublicTransport()
        timeout = PLAY_TIMEOUT if path.startswith('play/') else 30
        async with httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False, timeout=timeout) as client:
            async with client.stream('GET', base + 'api/' + path, params=params) as response:
                if response.status_code == 429:
                    retry = response.headers.get('retry-after', '')
                    try: delay = float(retry)
                    except ValueError:
                        try: delay = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                        except (TypeError, ValueError, OverflowError): delay = DEFAULT_COOLDOWN
                    if not 0 < delay < float('inf'): delay = DEFAULT_COOLDOWN
                    _cooldowns[base] = time.monotonic() + delay
                    raise RateLimitError('AnimePahe API rate limited the request; wait before retrying')
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    data.extend(chunk)
                    if len(data) > 2_000_000: raise ValueError('AnimePahe response is too large')
                result = json.loads(data)
                if not isinstance(result, dict): raise ValueError('Invalid AnimePahe response')
                if cacheable:
                    if len(_response_cache) >= MAX_CACHE_ENTRIES: _response_cache.pop(next(iter(_response_cache)))
                    _response_cache[cache_key] = (time.monotonic() + CACHE_SECONDS, copy.deepcopy(result))
                return result


async def search(base, query):
    data = await request(base, 'search', {'q':query})
    movies = []
    for item in data.get('data', [])[:20]:
        if not isinstance(item, dict) or not item.get('title'): continue
        try: session = identifier(item.get('session'))
        except ValueError: continue
        movies.append({'id':'animepahe:' + session, 'animepahe_session':session,
                       'name':str(item['title']), 'type':'series', 'addon':base,
                       'poster':item.get('poster'), 'year':item.get('year'),
                       'animepahe_episodes':True})
    return movies


def _release_videos(result):
    videos = []
    for item in result.get('data', []):
        if not isinstance(item, dict): continue
        try:
            ep = int(item['episode']); ep_id = identifier(item['session'])
            if ep <= 0 or str(ep) != str(item['episode']): continue
        except (KeyError, ValueError, TypeError): continue
        videos.append({'id':ep_id, 'season':1, 'episode':ep, 'title':item.get('title') or f'Episode {ep}'})
    return videos


async def details(base, movie):
    session = identifier(movie.get('animepahe_session'))
    path = quote(session, safe='') + '/releases'
    first = await request(base, path, {'sort':'episode_desc','page':1})
    info = first.get('paginationInfo') or {}
    try: total_pages = max(int(info.get('lastPage') or 1), 1)
    except (TypeError, ValueError, OverflowError): total_pages = 1
    pages = min(total_pages, 100)
    newest = _release_videos(first)
    oldest = newest if pages == 1 else _release_videos(await request(base, path, {'sort':'episode_desc','page':pages}))
    return dict(movie, videos=oldest, animepahe_episodes=True,
                animepahe_release_page=pages, animepahe_release_pages=pages,
                animepahe_newest_videos=newest, episodes_truncated=total_pages > pages)


async def release_page(base, movie, page):
    session = identifier(movie.get('animepahe_session'))
    pages = movie.get('animepahe_release_pages')
    if not isinstance(pages, int) or isinstance(pages, bool) or not 1 <= pages <= 100:
        raise ValueError('Invalid release page count')
    if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= pages:
        raise ValueError('Invalid release page')
    if page == movie.get('animepahe_release_page'):
        return movie
    if page == 1 and isinstance(movie.get('animepahe_newest_videos'), list):
        videos = movie['animepahe_newest_videos']
    else:
        result = await request(base, quote(session, safe='') + '/releases', {'sort':'episode_desc','page':page})
        videos = _release_videos(result)
    return dict(movie, videos=videos, animepahe_release_page=page)


async def streams(base, movie):
    session = identifier(movie.get('animepahe_session'))
    episode = identifier(movie.get('video_id'))
    data = await request(base, 'play/' + quote(session, safe=''), {'episodeId':episode})
    result, seen = [], set()
    for group, key in (('downloads','download'), ('sources','url')):
        items = data.get(group) or []
        if not isinstance(items, list): continue
        for item in items[:50]:
            if not isinstance(item, dict): continue
            url = item.get(key)
            manual = not isinstance(url, str) or not url.startswith(('https://','http://'))
            if manual:
                url = item.get('pahe') if group == 'downloads' else item.get('embed')
                if not isinstance(url, str) or len(url) > 8192: continue
                try:
                    parsed = httpx.URL(url)
                    if parsed.scheme != 'https' or not parsed.host or parsed.userinfo: continue
                    host = parsed.host.lower().rstrip('.')
                    if host == 'localhost' or host.endswith(('.localhost', '.local')): continue
                    try:
                        if not ipaddress.ip_address(host).is_global: continue
                    except ValueError:
                        if '.' not in host: continue
                except (ValueError, httpx.InvalidURL): continue
            if url in seen: continue
            seen.add(url)
            referer = item.get('downloadPage') if group == 'downloads' else item.get('embed')
            headers = {'Referer':referer} if isinstance(referer, str) and referer.startswith(('https://','http://')) else {}
            label = str((item.get('quality') if manual else None) or item.get('resolution') or item.get('quality') or 'unknown')
            if item.get('isDub'): label += ' English dub'
            result.append({'url':url, 'quality':label, 'filename':item.get('filename'), 'headers':headers,
                           'provider':'AnimePahe', 'source_manifest':base, 'manual_page':manual})
    return result
