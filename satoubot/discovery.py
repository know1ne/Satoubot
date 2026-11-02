"""Catalogue add-ons and Nuvio provider repository adapters."""
import asyncio
from functools import lru_cache
import subprocess
import json
import re
import shutil
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from .plugin_network import fetch_public

import httpx

HDHUB = 'https://hdhub.thevolecitor.qzz.io/eyJ0b3Jib3giOiJ1bnNldCIsInF1YWxpdGllcyI6IjIxNjBwLDEwODBwLDcyMHAiLCJzb3J0IjoiZGVzYyJ9/manifest.json'

DEFAULT_PROVIDERS = 'https://raw.githubusercontent.com/yoruix/nuvio-providers/refs/heads/main/manifest.json'


async def fetch_bytes(url, limit=8_000_000, headers=None):
    if urlparse(url).scheme not in ('https', 'http'):
        raise ValueError('Expected an HTTP(S) URL')
    async with httpx.AsyncClient(follow_redirects=True, timeout=25) as client:
        async with client.stream('GET', url, headers=headers) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError('Response too large')
            return bytes(data)


async def fetch_json(url):
    return json.loads(await fetch_bytes(url))


class Discovery:
    def __init__(self, store):
        self.store = store

    async def add(self, url):
        url = url.strip().replace('stremio://', 'https://', 1)
        if not urlparse(url).path.endswith('manifest.json'):
            url = url.rstrip('/') + '/manifest.json'
        manifest = await fetch_json(url)
        return self.register(url, manifest)

    def register(self, url, manifest):
        if urlparse(url).scheme not in ('http', 'https'):
            raise ValueError('Expected an HTTP(S) base URL')
        if not urlparse(url).path.endswith('manifest.json'):
            url = url.rstrip('/') + '/manifest.json'
        if not isinstance(manifest, dict):
            raise ValueError('Invalid manifest')
        kind = 'providers' if isinstance(manifest.get('scrapers'), list) else 'addon'
        if kind == 'addon' and not isinstance(manifest.get('catalogs', []), list):
            raise ValueError('Expected a Nuvio repository or catalogue add-on manifest')
        self.store.db.execute('INSERT OR REPLACE INTO manifests(url,kind,data) VALUES (?,?,?)',
                              (url, kind, json.dumps(manifest)))
        self.store.db.commit()
        return manifest.get('name', 'Add-on'), kind

    def manifests(self):
        return [(r['url'], r['kind'], json.loads(r['data'])) for r in self.store.db.execute('SELECT * FROM manifests')]

    async def search(self, query):
        results, errors = [], []
        for url, kind, manifest in self.manifests():
            if kind != 'addon': continue
            for catalog in manifest.get('catalogs', []):
                if catalog.get('type') != 'movie': continue
                extras = catalog.get('extra', [])
                if not any(e.get('name') == 'search' for e in extras if isinstance(e, dict)): continue
                try:
                    endpoint = f'catalog/movie/{quote(str(catalog["id"]), safe="")}/search={quote(query, safe="")}.json'
                    data = await fetch_json(urljoin(url, endpoint))
                    for meta in data.get('metas', [])[:20]:
                        if not isinstance(meta, dict) or not meta.get('id') or not meta.get('name'): continue
                        if any(m['id'] == str(meta['id']) for m in results): continue
                        results.append(dict(meta, id=str(meta['id']), addon=url))
                except Exception:
                    errors.append(manifest.get('name', 'Catalogue'))
        return results[:20], errors

    async def details(self, movie):
        data = await fetch_json(urljoin(movie['addon'], f'meta/movie/{quote(movie["id"], safe="")}.json'))
        meta = data.get('meta') or {}
        return dict(movie, **meta)

    async def streams(self, movie):
        streams, errors = [], []
        if not any(url == HDHUB for url, _, _ in self.manifests()):
            try: await self.add(HDHUB)
            except Exception: errors.append('HdHub add-on unavailable')
        if not any(kind == 'providers' for _, kind, _ in self.manifests()):
            try:
                await self.add(DEFAULT_PROVIDERS)
            except Exception:
                errors.append('Yoru providers could not be loaded')
        semaphore = asyncio.Semaphore(4)
        async def provider(url, entry):
            async with semaphore:
                try:
                    tmdb = movie.get('moviedb_id') or movie.get('tmdb_id')
                    if not tmdb and movie['id'].startswith('tmdb:'):
                        tmdb = movie['id'].split(':', 1)[1]
                    if not tmdb:
                        raise ValueError('Catalogue metadata has no TMDB ID for Nuvio providers')
                    code = (await fetch_bytes(urljoin(url, entry['filename']))).decode()
                    found = await run_plugin(code, str(tmdb))
                    for stream in found[:30]:
                        if isinstance(stream, dict) and isinstance(stream.get('url'), str):
                            streams.append(dict(stream, provider=entry.get('name', 'Provider')))
                except ProviderRuntimeError as exc:
                    if str(exc) not in errors: errors.append(str(exc))
                except Exception:
                    errors.append(entry.get('name', 'Provider'))
        jobs = []
        for url, kind, manifest in self.manifests():
            if kind == 'providers':
                jobs.extend(provider(url, p) for p in manifest['scrapers'] if p.get('enabled', True) and 'movie' in p.get('supportedTypes', []))
            else:
                resources = manifest.get('resources', [])
                if not any(r == 'stream' or isinstance(r, dict) and r.get('name') == 'stream' for r in resources): continue
                try:
                    data = await fetch_json(urljoin(url, f'stream/movie/{quote(movie["id"], safe="")}.json'))
                    for stream in data.get('streams', [])[:30]:
                        if isinstance(stream, dict) and isinstance(stream.get('url'), str):
                            headers = stream.get('behaviorHints', {}).get('proxyHeaders', {}).get('request', {})
                            hints=stream.get('behaviorHints',{})
                            description=str(stream.get('description') or stream.get('title') or '')
                            candidate=re.sub(r'^\s*(?:\[[^\]]*\]\s*)+','',description.split('\n')[0])
                            filename=hints.get('filename') or (candidate if re.search(r'\.(mkv|mp4|avi|webm)\b',candidate,re.I) else None)
                            streams.append(dict(stream, headers=stream.get('headers', headers), provider=manifest.get('name', 'Add-on'),size=stream.get('size',hints.get('videoSize')),filename=filename))
                except Exception:
                    errors.append(manifest.get('name', 'Add-on'))
        tasks = [asyncio.create_task(job) for job in jobs]
        if tasks:
            try:
                done, pending = await asyncio.wait(tasks, timeout=90)
                if pending: errors.append('Some providers timed out')
            finally:
                for task in tasks:
                    if not task.done(): task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        unique = {}
        for stream in streams:
            unique.setdefault(stream['url'], stream)
        return list(unique.values()), errors


class ProviderRuntimeError(RuntimeError):
    """An actionable provider runtime configuration problem."""


@lru_cache(maxsize=8)
def node_major(node):
    try:
        result = subprocess.run([node, '--version'], capture_output=True, text=True,
                                check=True, timeout=5)
        match = re.fullmatch(r'v(\d+)\.\d+\.\d+(?:[-+].*)?', result.stdout.strip())
        if not match:
            raise ValueError('Unexpected version')
        major = int(match.group(1))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ProviderRuntimeError('Plugin runtime: could not determine the Node.js version.') from exc
    if major < 24:
        raise ProviderRuntimeError(f'Plugin runtime: Node.js 24 or newer is required (found {major}).')
    return major


async def run_plugin(code, tmdb):
    runtime = (Path(__file__).parent / 'plugin_runtime').resolve()
    node, bwrap = shutil.which('node'), shutil.which('bwrap')
    if not node or not bwrap:
        raise ProviderRuntimeError('Plugin runtime: Node.js 24+ and bubblewrap are required for provider isolation.')
    node_major(node)
    # Node 25 restricts networking by default; the isolated worker only uses the
    # broker via stdio, so it does not need --allow-net on any Node version.
    # Only the runtime and system libraries are visible; the isolated worker has
    # its own network namespace. The host broker is its only network path.
    process = await asyncio.create_subprocess_exec(
        bwrap, '--unshare-user', '--unshare-net', '--unshare-pid', '--unshare-ipc', '--unshare-uts', '--cap-drop', 'ALL', '--die-with-parent', '--new-session',
        '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib', '--ro-bind', '/lib64', '/lib64',
        '--dev', '/dev', '--dir', '/tmp', '--ro-bind', str(runtime), '/app',
        '--ro-bind', node, '/node', '--chdir', '/app',
        '/node', '--permission', '--allow-fs-read=/app', '--max-old-space-size=128', '/app/runner.cjs',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env={'PATH': '/usr/bin:/bin'}, limit=2_100_000,
    )
    requests = set()
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(4)
    async def respond(message):
        try:
            async with semaphore:
                response = await fetch_public(message.get('url'), message.get('method'), message.get('headers'), message.get('body'))
            reply = {'id': message['id'], 'response': response}
        except Exception:
            reply = {'id': message['id'], 'error': 'Request failed or destination is not allowed'}
        async with lock:
            process.stdin.write((json.dumps(reply) + '\n').encode())
            await process.stdin.drain()
    async def run():
        process.stdin.write((json.dumps({'code': code, 'tmdb': tmdb}) + '\n').encode())
        await process.stdin.drain()
        count = 0
        while line := await process.stdout.readline():
            message = json.loads(line)
            if message.get('type') == 'result':
                result = message.get('results')
                if not isinstance(result, list): raise ValueError('Invalid provider results')
                return result
            if message.get('type') != 'fetch' or not isinstance(message.get('id'), int):
                raise ValueError('Invalid provider request')
            count += 1
            if count > 100: raise ValueError('Too many provider requests')
            task = asyncio.create_task(respond(message))
            requests.add(task)
            task.add_done_callback(requests.discard)
        raise ValueError('Provider failed or isolation unavailable')
    try:
        return await asyncio.wait_for(run(), 65)
    finally:
        for task in requests: task.cancel()
        if requests: await asyncio.gather(*requests, return_exceptions=True)
        if process.returncode is None:
            process.kill()
        await process.wait()
