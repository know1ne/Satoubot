import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs
from unittest.mock import AsyncMock, MagicMock

import pytest
from satoubot import animepahe
from satoubot.app import Bot
from satoubot.discovery import Discovery
from satoubot.file_info import clean_title
from satoubot.series import ordered_episodes
from satoubot.store import Store


@pytest.fixture
def api():
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            path=urlparse(self.path); query=parse_qs(path.query);calls.append((path.path, query))
            if path.path=='/api/search':
                body={'data':[{'session':'anime-123','title':'One Piece','year':1999,'poster':'https://img.test/p.jpg'}]}
            elif path.path=='/api/anime-123/releases':
                page=int(query.get('page',['1'])[0]); n=2 if page==1 else 1
                body={'paginationInfo':{'lastPage':2},'data':[{'session':f'episode-{n}','episode':n}]}
            elif path.path=='/api/play/anime-123':
                body={'downloads':[{'download':'https://cdn.test/video.mp4','downloadPage':'https://kwik.test/f/1','resolution':'720','isDub':True,'filename':'one.mp4'}], 'sources':[{'url':'https://cdn.test/video.m3u8','embed':'https://kwik.test/e/1','resolution':'720'}]}
            else:
                self.send_response(404);self.end_headers();return
            data=json.dumps(body).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield f'http://127.0.0.1:{server.server_port}/',calls
    server.shutdown();server.server_close();thread.join()


@pytest.mark.asyncio
async def test_builtin_search_paged_episodes_and_stream_identity(tmp_path,api):
    base,calls=api;store=Store(tmp_path/'db');animepahe.register_default(store,base)
    store.set_setting('source_selection_explicit','1');service=Discovery(store)
    results,errors=await service.search('One Piece')
    assert not errors and len(results)==1 and results[0]['animepahe_session']=='anime-123'
    movie=await service.details(results[0]);episodes=ordered_episodes(movie)
    assert [e['episode'] for e in episodes]==[1,2]
    assert clean_title(dict(movie,season=1,episode=2),{}).startswith('One Piece Episode 02')
    selected=dict(movie,season=1,episode=2,video_id='episode-2')
    prior_count=len(calls)
    assert await service.details(selected) == selected
    assert len(calls)==prior_count
    links,errors=await service.streams(selected)
    assert not errors and len(links)==2
    assert links[0]['headers']['Referer']=='https://kwik.test/f/1' and links[0]['quality']=='720 English dub'
    assert links[1]['headers']['Referer']=='https://kwik.test/e/1'
    assert ('/api/play/anime-123',{'episodeId':['episode-2']}) in calls
    assert ('/api/anime-123/releases',{'sort':['episode_desc'],'page':['2']}) in calls


@pytest.mark.asyncio
async def test_disabled_builtin_is_not_searched_and_persists(tmp_path,api):
    base,calls=api;store=Store(tmp_path/'db');animepahe.register_default(store,base)
    Discovery(store).toggle(base)
    animepahe.register_default(store,base)
    results,errors=await Discovery(store).search('One Piece')
    assert not results and not errors and not calls
    assert not Discovery(Store(tmp_path/'db')).enabled(base)


@pytest.mark.asyncio
async def test_unrelated_catalogue_does_not_query_animepahe(tmp_path,api,monkeypatch):
    base,calls=api;store=Store(tmp_path/'db');animepahe.register_default(store,base)
    store.set_setting('source_selection_explicit','1')
    result,errors=await Discovery(store).streams({'addon':'https://other.test/manifest.json','id':'tt123','type':'series','season':1,'episode':1,'video_id':'tt123:1:1'})
    assert not result and not calls


def test_api_base_and_identifiers_are_restricted():
    assert animepahe.normalize_base('http://localhost:3000')=='http://127.0.0.1:3000/'
    for bad in ('http://user:pass@localhost/', 'http://localhost/?token=x', 'file:///etc'):
        with pytest.raises(ValueError): animepahe.normalize_base(bad)
    for bad in ('../play','hello/secret','x?e=1'):
        with pytest.raises(ValueError): animepahe.identifier(bad)


@pytest.mark.asyncio
async def test_episode_labels_avoid_inventing_a_season(tmp_path):
    store=Store(tmp_path/'db');bot=Bot(store,tmp_path,42,MagicMock());bot.show_panel=AsyncMock()
    movie={'id':'animepahe:x','name':'Film','animepahe_episodes':True,'videos':[{'season':1,'episode':2,'id':'abc'}]}
    await bot.series_menu(SimpleNamespace(), movie, season=1)
    call=bot.show_panel.await_args
    assert 'Episodes' in call.args[1] and 'Season 1' not in call.args[1]
    assert call.kwargs['reply_markup'].inline_keyboard[0][0].text.startswith('Episode 02')


@pytest.mark.asyncio
async def test_api_retry_after_blocks_other_endpoints():
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            calls.append(self.path);self.send_response(429);self.send_header('Retry-After','60');self.end_headers()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}/'
    try:
        with pytest.raises(ValueError,match='rate limited'): await animepahe.request(base,'search',{'q':'film'})
        with pytest.raises(ValueError,match='wait'): await animepahe.request(base,'play/abc')
        assert len(calls)==1
    finally:
        animepahe._cooldowns.pop(base,None)
        await asyncio.to_thread(server.shutdown);server.server_close();thread.join()


@pytest.mark.asyncio
async def test_unreachable_api_reports_source_failure_without_breaking_search(tmp_path):
    store=Store(tmp_path/'db');animepahe.register_default(store,'http://127.0.0.1:1/')
    found,errors=await Discovery(store).search('One Piece')
    assert not found and errors==['AnimePahe (API)']


def test_changed_api_endpoint_preserves_disabled_state_without_duplicates(tmp_path):
    store=Store(tmp_path/'db');first=animepahe.register_default(store,'http://127.0.0.1:3000/')
    Discovery(store).toggle(first)
    second=animepahe.register_default(store,'http://127.0.0.1:4000/')
    sources=[(url,kind) for url,kind,_ in Discovery(store).manifests(active=False)]
    assert sources==[(second,'animepahe')]
    assert not Discovery(store).enabled(second)
