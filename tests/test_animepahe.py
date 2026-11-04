import asyncio
import json
import threading
import time
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


@pytest.fixture(autouse=True)
def fast_api_for_tests(monkeypatch):
    monkeypatch.setattr(animepahe, 'REQUEST_INTERVAL', 0)
    yield
    animepahe._cooldowns.clear()
    animepahe._next_requests.clear()
    animepahe._request_locks.clear()
    animepahe._response_cache.clear()


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
    assert [e['episode'] for e in episodes]==[1]
    latest = await animepahe.release_page(base, movie, 1)
    assert [e['episode'] for e in ordered_episodes(latest)] == [2]
    assert len([c for c in calls if c[0].endswith('/releases')]) == 2
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
        assert 55 < animepahe._cooldowns[base] - time.monotonic() <= 60
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


@pytest.mark.asyncio
async def test_large_catalog_loads_at_most_two_pages_and_navigates_in_numeric_order(monkeypatch):
    calls = []
    async def fake_request(base, path, params=None):
        page = params['page']; calls.append(page)
        return {'paginationInfo':{'lastPage':125}, 'data':[{'episode':126-page, 'session':f'ep-{page}'}]}
    monkeypatch.setattr(animepahe, 'request', fake_request)
    movie = await animepahe.details('http://127.0.0.1/', {'id':'animepahe:abc', 'animepahe_session':'abc'})
    assert calls == [1, 100] and movie['episodes_truncated']
    assert movie['videos'][0]['episode'] == 26
    middle = await animepahe.release_page('http://127.0.0.1/', movie, 99)
    assert middle['videos'][0]['episode'] == 27 and calls == [1, 100, 99]
    latest = await animepahe.release_page('http://127.0.0.1/', middle, 1)
    assert latest['videos'][0]['episode'] == 125 and calls == [1, 100, 99]
    episodes = []
    for page in range(100, 0, -1):
        selected = await animepahe.release_page('http://127.0.0.1/', movie, page)
        episodes.extend(v['episode'] for v in ordered_episodes(selected))
    assert episodes == list(range(26, 126))
    with pytest.raises(ValueError): await animepahe.release_page('http://127.0.0.1/', movie, 101)


@pytest.mark.asyncio
async def test_episode_page_navigation_fetches_only_selected_page(tmp_path, monkeypatch):
    store = Store(tmp_path/'db'); base = animepahe.register_default(store)
    bot = Bot(store, tmp_path, 42, MagicMock()); bot.show_panel = AsyncMock()
    movie = {'addon':base, 'id':'animepahe:abc', 'animepahe_session':'abc', 'name':'Example',
             'animepahe_episodes':True, 'animepahe_release_page':3, 'animepahe_release_pages':3,
             'animepahe_newest_videos':[{'id':'ep-3','season':1,'episode':3}],
             'videos':[{'id':'ep-1','season':1,'episode':1}]}
    await bot.series_menu(SimpleNamespace(), movie, season=1)
    rows = bot.show_panel.await_args.kwargs['reply_markup'].inline_keyboard
    later = next(b.callback_data for row in rows for b in row if b.text == 'Later episodes')
    request = AsyncMock(return_value={'data':[{'session':'ep-2', 'episode':2}]})
    monkeypatch.setattr(animepahe, 'request', request)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))))
    await bot.series_callback(SimpleNamespace(callback_query=SimpleNamespace(data=later, message=MagicMock())), context)
    await asyncio.gather(*bot.discovery_tasks)
    assert request.await_count == 1 and request.await_args.args[2]['page'] == 2
    rows = bot.show_panel.await_args.kwargs['reply_markup'].inline_keyboard
    assert rows[0][0].text.startswith('Episode 02')


@pytest.mark.asyncio
async def test_requests_are_spaced_and_rate_limit_stops_queued_requests(monkeypatch):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            calls.append(time.monotonic())
            if len(calls) == 2:
                self.send_response(429); self.end_headers(); return
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"data":[]}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}/'
    monkeypatch.setattr(animepahe, 'REQUEST_INTERVAL', 0.1)
    try:
        results = await asyncio.gather(*(animepahe.request(base, 'search', {'q':str(i)}) for i in range(3)), return_exceptions=True)
        assert len(calls) == 2 and calls[1] - calls[0] >= 0.07
        assert isinstance(results[1], animepahe.RateLimitError)
        assert isinstance(results[2], animepahe.RateLimitError)
        assert animepahe._cooldowns[base] - time.monotonic() > 290
        with pytest.raises(animepahe.RateLimitError): await animepahe.request(base, 'play/abc')
        assert len(calls) == 2
    finally:
        await asyncio.to_thread(server.shutdown); server.server_close(); thread.join()


@pytest.mark.asyncio
async def test_repeated_search_and_release_requests_are_cached_but_play_is_not(api):
    base, calls = api
    first, second = await asyncio.gather(*(animepahe.request(base, 'search', {'q':'One Piece'}) for _ in range(2)))
    assert len(calls) == 1 and first == second
    first['data'].clear()
    assert len((await animepahe.request(base, 'search', {'q':'One Piece'}))['data']) == 1
    await animepahe.request(base, 'anime-123/releases', {'page':1, 'sort':'episode_desc'})
    await animepahe.request(base, 'anime-123/releases', {'sort':'episode_desc', 'page':1})
    assert len([call for call in calls if call[0].endswith('/releases')]) == 1
    await animepahe.request(base, 'play/anime-123')
    await animepahe.request(base, 'play/anime-123')
    assert len([call for call in calls if call[0].startswith('/api/play/')]) == 2


@pytest.mark.asyncio
async def test_rate_limit_is_reported_to_search_user(tmp_path, monkeypatch):
    store = Store(tmp_path/'db'); animepahe.register_default(store)
    monkeypatch.setattr(animepahe, 'search', AsyncMock(side_effect=animepahe.RateLimitError('wait')))
    bot = Bot(store, tmp_path, 42, MagicMock())
    message = MagicMock(reply_text=AsyncMock(return_value=MagicMock(edit_text=AsyncMock())))
    await bot.begin_search(message, 'One Piece')
    await asyncio.gather(*bot.discovery_tasks)
    assert 'rate limiting requests' in bot.panel().text


@pytest.mark.asyncio
async def test_expired_cache_is_refetched(api, monkeypatch):
    base, calls = api
    monkeypatch.setattr(animepahe, 'CACHE_SECONDS', 0)
    await animepahe.request(base, 'search', {'q':'One Piece'})
    await animepahe.request(base, 'search', {'q':'One Piece'})
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_unresolved_download_qualities_are_manual_pages_not_media(monkeypatch):
    data = {'downloads':[
        {'download':None, 'pahe':'https://download.test/480', 'quality':'SCY · 480p'},
        {'download':None, 'pahe':'https://download.test/1080', 'quality':'SCY · 1080p'},
        {'download':None, 'pahe':'http://127.0.0.1/private', 'quality':'unsafe'},
        {'download':None, 'pahe':'https://user:password@download.test/path', 'quality':'unsafe'},
    ], 'sources':[{'url':None, 'embed':'https://embed.test/e/abc', 'resolution':'1080'}]}
    monkeypatch.setattr(animepahe, 'request', AsyncMock(return_value=data))
    movie = {'animepahe_session':'title', 'video_id':'episode'}
    results = await animepahe.streams('http://127.0.0.1:3000/', movie)
    assert [v['quality'] for v in results] == ['SCY · 480p', 'SCY · 1080p', '1080']
    assert all(v['manual_page'] for v in results)
    assert all('password' not in v['url'] for v in results)


@pytest.mark.asyncio
async def test_manual_page_is_shown_without_probing_or_creating_transfer(tmp_path, monkeypatch):
    bot = Bot(Store(tmp_path/'db'), tmp_path, 42, MagicMock())
    probe = AsyncMock(side_effect=AssertionError('Manual pages must not be probed'))
    monkeypatch.setattr('satoubot.catalogue.probe', probe)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    await bot.prepare_choice(context, {'url':'https://download.test/480', 'manual_page':True,
                                       'movie':{'name':'Example'}, 'quality':'480p'})
    assert 'manually' in context.bot.send_message.await_args.args[1]
    assert 'https://download.test/480' in context.bot.send_message.await_args.args[1]
    probe.assert_not_awaited()
    assert not bot.tasks


@pytest.mark.asyncio
async def test_manual_quality_options_reach_the_episode_card(tmp_path, monkeypatch):
    store = Store(tmp_path/'db'); base = animepahe.register_default(store)
    bot = Bot(store, tmp_path, 42, MagicMock())
    movie = {'id':'animepahe:title', 'addon':base, 'animepahe_session':'title',
             'name':'Example', 'type':'series', 'season':1, 'episode':1, 'video_id':'episode', 'animepahe_episodes':True}
    key = store.cache('movie', movie)
    monkeypatch.setattr(animepahe, 'request', AsyncMock(return_value={'downloads':[
        {'download':None, 'pahe':'https://download.test/480', 'quality':'SCY · 480p'},
        {'download':None, 'pahe':'https://download.test/1080', 'quality':'SCY · 1080p'},
    ], 'sources':[]}))
    monkeypatch.setattr('satoubot.catalogue.probe', AsyncMock(side_effect=AssertionError('Must not probe pages')))
    store.set_setting('source_selection_explicit', '1')
    card = MagicMock(edit_text=AsyncMock())
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(return_value=card)))
    await bot.movie_card(context, key)
    last = card.edit_text.await_args
    assert 'Available options: 2' in last.args[0]
    buttons = [b.text for row in last.kwargs['reply_markup'].inline_keyboard for b in row]
    assert any('Manual SCY · 480p' in b for b in buttons)
    assert any('Manual SCY · 1080p' in b for b in buttons)


@pytest.mark.asyncio
async def test_cache_is_bounded_and_evicted_search_is_refetched(api):
    base, calls = api
    for n in range(animepahe.MAX_CACHE_ENTRIES + 1):
        await animepahe.request(base, 'search', {'q':str(n)})
    assert len(animepahe._response_cache) == animepahe.MAX_CACHE_ENTRIES
    await animepahe.request(base, 'search', {'q':'0'})
    assert len(calls) == animepahe.MAX_CACHE_ENTRIES + 2


@pytest.mark.asyncio
async def test_bot_search_with_only_builtin_animepahe(tmp_path, api):
    base, calls = api
    bot = Bot(Store(tmp_path/'db'), tmp_path, 42, MagicMock())
    animepahe.register_default(bot.store, base)
    message = MagicMock(reply_text=AsyncMock(return_value=MagicMock(edit_text=AsyncMock())))
    await bot.begin_search(message, 'One Piece')
    await asyncio.gather(*bot.discovery_tasks)
    assert ('/api/search', {'q':['One Piece']}) in calls
    assert bot.panel().markup.inline_keyboard[0][0].text.startswith('One Piece')


@pytest.mark.asyncio
async def test_bot_search_with_disabled_builtin_requires_source(tmp_path, api):
    base, calls = api
    bot = Bot(Store(tmp_path/'db'), tmp_path, 42, MagicMock())
    animepahe.register_default(bot.store, base)
    Discovery(bot.store).toggle(base)
    message = MagicMock(reply_text=AsyncMock())
    await bot.begin_search(message, 'One Piece')
    assert 'Enable AnimePahe' in message.reply_text.await_args.args[0]
    assert not calls and not bot.panel().pending


@pytest.mark.asyncio
async def test_bot_reports_builtin_failure_without_requesting_manifest(tmp_path, monkeypatch):
    bot = Bot(Store(tmp_path/'db'), tmp_path, 42, MagicMock())
    animepahe.register_default(bot.store)
    monkeypatch.setattr(animepahe, 'search', AsyncMock(side_effect=ValueError('API unavailable')))
    message = MagicMock(reply_text=AsyncMock(return_value=MagicMock(edit_text=AsyncMock())))
    await bot.begin_search(message, 'One Piece')
    await asyncio.gather(*bot.discovery_tasks)
    assert 'AnimePahe API failed or timed out' in bot.panel().text
    assert 'No manifest is needed for AnimePahe' in bot.panel().text
