"""Adapter for a separately hosted AnimePahe API; no upstream code is executed here."""
import asyncio
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
    remaining = _cooldowns.get(base, 0) - time.monotonic()
    if remaining > 0: raise ValueError('AnimePahe API requested a wait before retrying')
    transport = None if _loopback(httpx.URL(base).host) else PublicTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False, timeout=30) as client:
        async with client.stream('GET', base + 'api/' + path, params=params) as response:
            if response.status_code == 429:
                retry = response.headers.get('retry-after', '')
                try: delay = float(retry)
                except ValueError:
                    try: delay = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                    except (TypeError, ValueError, OverflowError): delay = 60
                _cooldowns[base] = time.monotonic() + max(0, delay)
                raise ValueError('AnimePahe API rate limited the request')
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=8192):
                data.extend(chunk)
                if len(data) > 2_000_000: raise ValueError('AnimePahe response is too large')
            result = json.loads(data)
            if not isinstance(result, dict): raise ValueError('Invalid AnimePahe response')
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


async def details(base, movie):
    session = identifier(movie.get('animepahe_session'))
    path = quote(session, safe='') + '/releases'
    first = await request(base, path, {'sort':'episode_desc','page':1})
    info = first.get('paginationInfo') or {}
    try: total_pages = max(int(info.get('lastPage') or 1), 1)
    except (TypeError, ValueError): total_pages = 1
    pages = min(total_pages, 100)
    semaphore = asyncio.Semaphore(2)
    async def page(n):
        async with semaphore:
            return await request(base, path, {'sort':'episode_desc','page':n})
    results = [first]
    if pages > 1: results.extend(await asyncio.gather(*(page(n) for n in range(2, pages+1))))
    videos = []
    for result in results:
        for item in result.get('data', []):
            if not isinstance(item, dict): continue
            try:
                ep = int(item['episode']); ep_id = identifier(item['session'])
                if ep <= 0 or str(ep) != str(item['episode']): continue
            except (KeyError, ValueError, TypeError): continue
            videos.append({'id':ep_id, 'season':1, 'episode':ep, 'title':item.get('title') or f'Episode {ep}'})
    return dict(movie, videos=videos, animepahe_episodes=True, episodes_truncated=total_pages > pages)


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
            if not isinstance(url, str) or not url.startswith(('https://','http://')) or url in seen: continue
            seen.add(url)
            referer = item.get('downloadPage') if group == 'downloads' else item.get('embed')
            headers = {'Referer':referer} if isinstance(referer, str) and referer.startswith(('https://','http://')) else {}
            label = str(item.get('resolution') or item.get('quality') or 'unknown')
            if item.get('isDub'): label += ' English dub'
            result.append({'url':url, 'quality':label, 'filename':item.get('filename'), 'headers':headers,
                           'provider':'AnimePahe', 'source_manifest':base})
    return result
