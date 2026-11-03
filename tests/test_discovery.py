import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from satoubot.app import Bot
from satoubot.discovery import Discovery, run_plugin
from satoubot.store import Store


@pytest.mark.asyncio
async def test_catalogue_search_and_metadata_protocol_and_persistence(tmp_path, monkeypatch):
    replies = {
        'https://catalogue.test/manifest.json': {'name': 'Catalogue', 'catalogs': [{'id': 'top', 'type': 'movie', 'extra': [{'name': 'search'}]}]},
        'https://catalogue.test/catalog/movie/top/search=Insidious%20movie.json': {'metas': [{'id': 'tt1591095', 'name': 'Insidious', 'year': '2011'}]},
        'https://catalogue.test/meta/movie/tt1591095.json': {'meta': {'name': 'Insidious', 'moviedb_id': 49018, 'background': 'https://images.test/background.jpg'}},
    }
    fetch = AsyncMock(side_effect=lambda url: replies[url])
    monkeypatch.setattr('satoubot.discovery.fetch_json', fetch)
    store = Store(tmp_path / 'db')
    await Discovery(store).add('https://catalogue.test/manifest.json')
    service = Discovery(Store(tmp_path / 'db'))
    movies, errors = await service.search('Insidious movie')
    assert not errors and len(movies) == 1
    meta = await service.details(movies[0])
    assert meta['moviedb_id'] == 49018 and meta['year'] == '2011'
    assert meta['background'].endswith('background.jpg')
    assert store.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_real_nuvio_worker_and_restricted_modules():
    code = 'module.exports.getStreams = async id => [{url: "https://example.test/"+id, quality: "720p", headers:{Referer:"https://example.test"}}];'
    result = await run_plugin(code, '49018')
    assert result[0]['url'].endswith('/49018')
    assert result[0]['headers']['Referer'] == 'https://example.test'
    with pytest.raises(ValueError):
        await run_plugin('require("node:fs").readFileSync("/etc/passwd");', '1')


@pytest.mark.asyncio
async def test_selected_file_prepares_name_headers_and_requires_upload_approval(tmp_path, monkeypatch):
    monkeypatch.setattr('satoubot.catalogue.probe', AsyncMock(return_value={'status':'ready','size':100,'filename':'movie.mp4','content_type':'video/mp4'}))
    store = Store(tmp_path / 'db')
    bot = Bot(store, tmp_path, 42, MagicMock())
    async def downloaded(context, key):
        store.update(key, state='review', path=str(tmp_path/'movie.mp4'))
    bot.perform_download = AsyncMock(side_effect=downloaded)
    context = MagicMock(); context.bot.send_message = AsyncMock()
    stream = {'url': 'https://source.test/movie.mp4', 'quality': '720p HEVC', 'file_type': 'mp4',
              'headers': {'Referer': 'https://source.test'}, 'movie': {'name': 'Insidious', 'year': '2011'}}
    await bot.download_choice(context, stream)
    row = store.find_source(stream['url'])
    assert row['state'] == 'review'
    assert row['filename'] == 'Insidious 2011 720P HEVC.mp4'
    assert json.loads(store.setting(f'job:{row["id"]}:headers')) == stream['headers']
    assert row['telegram_message_id'] is None
    await bot.download_choice(context, stream)
    bot.perform_download.assert_awaited_once()


@pytest.mark.asyncio
async def test_file_callback_respects_off_and_unknown_size_never_downloads(tmp_path, monkeypatch):
    store = Store(tmp_path/'db'); store.set_setting('transfers_enabled', '0')
    stream = {'url': 'https://source.test/file.mp4', 'movie': {'name': 'Example'}, 'file_type': 'mp4'}
    key = store.cache('stream', stream)
    bot = Bot(store, tmp_path, 42, MagicMock())
    q = MagicMock(); q.data = f'file:{key}'; q.answer = AsyncMock(); q.message.reply_text = AsyncMock()
    update = MagicMock(); update.effective_user.id=42; update.effective_chat.type='private'; update.callback_query=q
    context=MagicMock();context.bot.send_message=AsyncMock()
    bot.prepare_choice=AsyncMock()
    await bot.callback(update, context)
    await asyncio.gather(*bot.discovery_tasks)
    bot.prepare_choice.assert_awaited_once()
    assert not bot.tasks
    # Transfer approval itself remains gated while off.
    confirmation=store.cache('confirmation',stream)
    q.data=f'yes:{confirmation}'
    await bot.callback(update,context)
    assert not bot.tasks and 'off' in q.message.reply_text.await_args.args[0]
    del bot.prepare_choice
    context = MagicMock(); context.bot.send_message=AsyncMock()
    await bot.download_choice(context, stream)
    assert not store.find_source(stream['url'])


@pytest.mark.asyncio
async def test_inline_queries_are_private_to_admin(tmp_path):
    bot=Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock())
    update=MagicMock(); update.inline_query.from_user.id=43; update.inline_query.answer=AsyncMock()
    await bot.inline_search(update,MagicMock())
    update.inline_query.answer.assert_awaited_once_with([],cache_time=0,is_personal=True)


@pytest.mark.asyncio
async def test_movie_card_uses_landscape_and_buttons_without_starting_download(tmp_path, monkeypatch):
    import io
    movie={'id':'tt1','name':'Example','year':'2026','background':'https://images.test/bg.jpg','addon':'https://addon.test/manifest.json'}
    store=Store(tmp_path/'db'); key=store.cache('movie',movie)
    bot=Bot(store,tmp_path,42,MagicMock())
    monkeypatch.setattr(Discovery,'details',AsyncMock(return_value=movie))
    monkeypatch.setattr(Discovery,'streams',AsyncMock(return_value=([{'url':'https://file.test/movie.mkv','quality':'1080p HEVC'}],[])))
    monkeypatch.setattr('satoubot.catalogue.probe',AsyncMock(return_value={'size':800_000_000,'content_type':'video/x-matroska','filename':'movie.mkv'}))
    monkeypatch.setattr('satoubot.catalogue.landscape',AsyncMock(return_value=io.BytesIO(b'jpeg')))
    context=MagicMock();context.bot.send_photo=AsyncMock()
    await bot.movie_card(context,key)
    call=context.bot.send_photo.await_args.kwargs
    assert 'Example' in call['caption'] and '2026' in call['caption']
    label=call['reply_markup'].inline_keyboard[0][0].text
    assert '1080p HEVC' in label and '800.0 MB' in label and 'mkv' in label
    assert store.db.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
    assert not bot.tasks


@pytest.mark.asyncio
async def test_download_preserves_required_source_headers(tmp_path, monkeypatch):
    monkeypatch.setattr('satoubot.public_transport.public_address', AsyncMock(return_value='127.0.0.1'))
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from satoubot.transfer import inspect_url, download
    received=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_HEAD(self):
            received.append(self.headers.get('Referer'))
            self.send_response(200);self.send_header('Content-Length','5');self.end_headers()
        def do_GET(self):
            received.append(self.headers.get('Referer'))
            self.send_response(200);self.send_header('Content-Length','5');self.end_headers();self.wfile.write(b'media')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        url=f'http://127.0.0.1:{server.server_port}/file'
        headers={'Referer':'https://required.test'}
        size,_=await inspect_url(url,headers)
        await download(url,tmp_path/'file',size,AsyncMock(),headers=headers)
        assert (tmp_path/'file').read_bytes()==b'media'
        assert received==['https://required.test']*2
    finally:
        await asyncio.to_thread(server.shutdown);server.server_close();thread.join()


def test_expired_selection_ids_are_never_reassigned(tmp_path):
    store=Store(tmp_path/'db')
    old=store.cache('pages',{'url':'https://old.test'})
    store.db.execute("UPDATE discovery_cache SET created_at='2000-01-01'")
    store.db.commit()
    assert store.cached(old,'pages') is None
    store=Store(tmp_path/'db')
    new=store.cache('pages',{'url':'https://new.test'})
    assert new != old


@pytest.mark.asyncio
async def test_uploaded_manifest_requires_base_and_is_saved(tmp_path):
    store=Store(tmp_path/'db');bot=Bot(store,tmp_path,42,MagicMock())
    context=MagicMock();context.user_data={'manifest_pending':True}
    update=MagicMock();update.effective_user.id=42;update.effective_chat.type='private';update.message.reply_text=AsyncMock()
    manifest={'name':'Uploaded catalogue','catalogs':[]}
    remote=MagicMock();remote.download_as_bytearray=AsyncMock(return_value=bytearray(json.dumps(manifest).encode()))
    update.message.document.file_size=100;update.message.document.get_file=AsyncMock(return_value=remote)
    await bot.manifest_document(update,context)
    assert context.user_data['uploaded_manifest']==manifest
    assert not Discovery(store).manifests()
    update.message.text='https://catalogue.test'
    await bot.text(update,context)
    assert Discovery(store).manifests()[0][0]=='https://catalogue.test/manifest.json'


@pytest.mark.asyncio
async def test_yes_no_confirmation_and_duplicate_yes_never_double_download(tmp_path,monkeypatch):
    import asyncio
    store=Store(tmp_path/'db');bot=Bot(store,tmp_path,42,MagicMock())
    stream={'url':'https://file.test/movie.mkv','quality':'720p','file_type':'mkv','movie':{'name':'Insidious','year':'2011'}}
    monkeypatch.setattr('satoubot.catalogue.probe',AsyncMock(return_value={'size':100,'filename':'Insidious.720p.Hindi_English.mkv','content_type':'video/x-matroska'}))
    context=MagicMock();context.bot.send_message=AsyncMock()
    bot.download_choice=AsyncMock()
    await bot.prepare_choice(context,stream)
    assert store.db.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
    keyboard=context.bot.send_message.await_args.kwargs['reply_markup']
    yes=keyboard.inline_keyboard[0][0].callback_data
    q=MagicMock();q.data=yes;q.answer=AsyncMock();q.edit_message_reply_markup=AsyncMock();q.message.reply_text=AsyncMock()
    update=MagicMock();update.effective_user.id=42;update.effective_chat.type='private';update.callback_query=q
    await bot.callback(update,context)
    await asyncio.gather(*bot.tasks)
    await bot.callback(update,context)
    bot.download_choice.assert_awaited_once()
    await bot.prepare_choice(context,stream)
    q.data=context.bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard[0][1].callback_data
    await bot.callback(update,context)
    bot.download_choice.assert_awaited_once()


@pytest.mark.asyncio
async def test_large_source_returns_link_even_when_off_without_probe_or_download(tmp_path,monkeypatch):
    store=Store(tmp_path/'db');store.set_setting('transfers_enabled','0')
    bot=Bot(store,tmp_path,42,MagicMock())
    probe_mock=AsyncMock(return_value={'status':'ready','size':10_000_000_001,'filename':'large.mkv','content_type':'video/x-matroska'});monkeypatch.setattr('satoubot.catalogue.probe',probe_mock)
    context=MagicMock();context.bot.send_message=AsyncMock()
    await bot.prepare_choice(context,{'url':'https://file.test/large.mkv','size':10_000_000_001,'movie':{'name':'Example'},'quality':'1080p'})
    assert 'Download it manually' in context.bot.send_message.await_args.args[1]
    assert 'https://file.test/large.mkv' in context.bot.send_message.await_args.args[1]
    probe_mock.assert_awaited_once()
    assert store.db.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
    assert store.db.execute('SELECT count(*) FROM source_links').fetchone()[0]==1


def test_pagination_four_file_choices_and_persistent_dedup(tmp_path):
    store=Store(tmp_path/'db');bot=Bot(store,tmp_path,42,MagicMock())
    ids=[]
    for n in range(6):ids.append(store.cache('stream',{'url':f'https://file.test/{n}','quality':'720p','size':100}))
    page=store.cache('pages',ids)
    rows=bot.file_page(page,ids,0).inline_keyboard
    assert len([r for r in rows if r[0].callback_data.startswith('file:')])==4
    assert rows[-2][0].text=='More' and any(button.text=='Cancel' for button in rows[-1])
    rows=bot.file_page(page,ids,1).inline_keyboard
    assert len([r for r in rows if r[0].callback_data.startswith('file:')])==2
    one=store.cache('stream',{'url':'https://one.test','identity':'movie:filename:100'})
    two=store.cache('stream',{'url':'https://two.test','identity':'movie:filename:100'})
    assert one==two
    assert Store(tmp_path/'db').cached(one,'stream')['url']=='https://two.test'


@pytest.mark.asyncio
async def test_owned_destinations_require_bot_admin_and_selection_needs_fresh_approval(tmp_path,monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr('satoubot.destinations.get_peer_id',lambda entity:entity.id)
    store=Store(tmp_path/'db');uploader=MagicMock()
    uploader.get_dialogs=AsyncMock(return_value=[SimpleNamespace(name='Owned',entity=SimpleNamespace(id=-1001,creator=True,broadcast=True)),SimpleNamespace(name='Other',entity=SimpleNamespace(id=-1002,creator=False,broadcast=True))])
    bot=Bot(store,tmp_path,42,uploader)
    context=MagicMock();context.bot.id=123;context.bot.send_message=AsyncMock();context.bot.get_chat_member=AsyncMock(return_value=SimpleNamespace(status='administrator',can_post_messages=True))
    await bot.destination_menu(context)
    rows=context.bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard
    assert len(rows)==2 and rows[0][0].text=='Owned'
    assert [button.text for button in rows[-1]] == ['Back', 'Cancel', 'Main menu']
    q=MagicMock();q.data=rows[0][0].callback_data;q.edit_message_reply_markup=AsyncMock();q.message.reply_text=AsyncMock()
    update=MagicMock();update.callback_query=q
    await bot.select_destination(update,context)
    assert store.setting('target')=='-1001'
    assert not bot.tasks


@pytest.mark.asyncio
async def test_progress_ticks_every_five_seconds_and_completion_keeps_clean_name(tmp_path,monkeypatch):
    import asyncio
    store=Store(tmp_path/'db');job=store.create('https://source.test','Example','Example 2026 720P.mkv',100)
    store.set_setting(f'job:{job}:original','Example.2026_720p.mkv')
    bot=Bot(store,tmp_path,42,MagicMock());bot.preview=AsyncMock()
    edited=asyncio.Event();intervals=[];sleep=asyncio.sleep
    async def quick_sleep(seconds):
        intervals.append(seconds);await sleep(.001)
    monkeypatch.setattr('satoubot.download.asyncio.sleep',quick_sleep)
    async def edit(text):
        if 'Speed:' in text:
            assert 'Example.2026_720p.mkv' in text and 'ETA:' in text
            edited.set()
    context=MagicMock();context.bot.send_message=AsyncMock(return_value=MagicMock(edit_text=AsyncMock(side_effect=edit)))
    async def fake_download(url,path,size,progress,headers):
        await progress(50,100,10)
        await asyncio.wait_for(edited.wait(),1)
        path.write_bytes(b'media')
    monkeypatch.setattr('satoubot.download.download',fake_download)
    monkeypatch.setattr('satoubot.download.probe',AsyncMock(return_value={'status':'ready','size':100}))
    await bot.perform_download(context,job)
    assert 5 in intervals
    assert store.get(job)['state']=='review'
    assert Path(store.get(job)['path']).name=='Example 2026 720P.mkv'
    assert any('Download done' in c.args[1] for c in context.bot.send_message.await_args_list)


@pytest.mark.asyncio
async def test_destination_lookup_failure_keeps_existing_target(tmp_path):
    from telegram.error import BadRequest
    store=Store(tmp_path/'db'); store.set_setting('target','-1001')
    key=store.cache('destination',{'target':-1002,'job':0,'title':'Other'})
    bot=Bot(store,tmp_path,42,MagicMock())
    q=MagicMock(); q.data=f'dest:{key}'; q.message.reply_text=AsyncMock(); q.edit_message_reply_markup=AsyncMock()
    update=MagicMock(); update.callback_query=q
    context=MagicMock(); context.bot.get_chat_member=AsyncMock(side_effect=BadRequest('chat not found'))
    await bot.select_destination(update,context)
    assert store.setting('target') == '-1001'
    q.message.reply_text.assert_awaited_once()
    q.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_error_is_reported_in_movie_card(tmp_path, monkeypatch):
    from satoubot.discovery import HDHUB, ProviderRuntimeError
    store=Store(tmp_path/'db'); service=Discovery(store)
    service.register(HDHUB, {'name':'HdHub','catalogs':[]})
    service.register('https://repo.test/manifest.json', {'name':'Plugins','scrapers':[{'filename':'provider.js','supportedTypes':['movie']}]})
    monkeypatch.setattr('satoubot.discovery.fetch_bytes', AsyncMock(return_value=b'provider'))
    monkeypatch.setattr('satoubot.discovery.run_plugin', AsyncMock(side_effect=ProviderRuntimeError('Plugin runtime: Node.js 24 or newer is required (found 22).')))
    movie={'id':'tt1','name':'Example','moviedb_id':1}
    streams, errors=await service.streams(movie)
    assert not streams
    assert errors == ['Plugin runtime: Node.js 24 or newer is required (found 22).']
    key=store.cache('movie', movie); bot=Bot(store,tmp_path,42,MagicMock())
    monkeypatch.setattr(Discovery,'details',AsyncMock(return_value=movie))
    context=MagicMock(); context.bot.send_message=AsyncMock()
    await bot.movie_card(context,key)
    assert 'Node.js 24 or newer is required' in context.bot.send_message.await_args.args[1]


def test_downloadable_results_first_across_pages_without_losing_manual_or_failures(tmp_path):
    store=Store(tmp_path/'db');bot=Bot(store,tmp_path,42,MagicMock())
    def cache(n,status,size,kind='stream'):
        return store.cache(kind, {'url':f'https://file.test/{n}', 'quality':str(n), 'size':size, 'check_status':status})
    manual=cache('manual','manual',100)
    dead=-cache('dead','unavailable',None,'unchecked')
    large=cache('large','ready',10_000_000_001)
    ready1=cache('first','ready',100)
    unknown=cache('unknown','ready',None)
    ready2=cache('second','ready',200)
    ids=[manual,dead,large,ready1,unknown,ready2]
    page=store.cache('pages',ids)
    first=bot.file_page(page,ids,0).inline_keyboard
    assert [row[0].callback_data for row in first[:4]] == [f'file:{ready1}',f'file:{ready2}',f'file:{manual}',f'retryfile:{-dead}']
    assert first[0][0].text.startswith('DW ')
    assert first[2][0].text.startswith('Manual ') and first[3][0].text.startswith('Unavailable ')
    second=bot.file_page(page,ids,1).inline_keyboard
    assert [row[0].callback_data for row in second[:2]] == [f'file:{large}',f'file:{unknown}']
    assert second[0][0].text.startswith('Link ') and second[1][0].text.startswith('Size unknown ')
