from unittest.mock import AsyncMock
import pytest
from satoubot.discovery import Discovery
from satoubot.media_identity import supports_resource, resolve_tmdb
from satoubot.store import Store


def test_resource_types_and_prefixes_follow_manifest_defaults_and_overrides():
    manifest = {'types':['movie'], 'idPrefixes':['tt'], 'resources':['stream', {'name':'meta','types':['series'],'idPrefixes':['anime:']}]}
    assert supports_resource(manifest, 'stream', 'movie', 'tt123')
    assert not supports_resource(manifest, 'stream', 'series', 'tt123:1:1')
    assert not supports_resource(manifest, 'stream', 'movie', 'anime:1')
    assert supports_resource(manifest, 'meta', 'series', 'anime:1')
    assert not supports_resource(manifest, 'meta', 'series', 'tt123')


@pytest.mark.asyncio
async def test_catalogue_mapping_uses_same_title_and_enabled_sources(tmp_path, monkeypatch):
    service = Discovery(Store(tmp_path/'db'))
    for host in ('first', 'wrong', 'disabled', 'good'):
        service.register(f'https://{host}.test/manifest.json', {'resources':['meta'], 'types':['series'], 'idPrefixes':['tt']})
    service.toggle('https://disabled.test/manifest.json')
    calls = []
    async def fetch(url):
        calls.append(url)
        if 'first.test' in url: return {'meta':{'id':'tt123', 'name':'Example'}}
        if 'wrong.test' in url: return {'meta':{'id':'tt999', 'tmdb_id':999}}
        return {'meta':{'id':'tt123', 'moviedb_id':123}}
    monkeypatch.setattr('satoubot.discovery.fetch_json', fetch)
    movie = {'id':'tt123', 'type':'series', 'season':2, 'episode':10}
    assert await resolve_tmdb(service, movie) == '123'
    assert await resolve_tmdb(service, movie) == '123'
    assert len(calls) == 3 and all('disabled.test' not in url for url in calls)
    assert all('/meta/series/tt123.json' in url for url in calls)
    assert await resolve_tmdb(service, {'id':'tmdb:456'}) == '456'
    assert await resolve_tmdb(service, {'id':'custom','tmdb_id':789}) == '789'


@pytest.mark.asyncio
async def test_mapping_never_guesses_or_uses_tmdb_credentials(tmp_path, monkeypatch):
    service = Discovery(Store(tmp_path/'db'))
    service.register('https://metadata.test/manifest.json', {'resources':['meta'], 'types':['movie']})
    fetch = AsyncMock(return_value={'meta':{'name':'Similar title'}})
    monkeypatch.setattr('satoubot.discovery.fetch_json', fetch)
    monkeypatch.setenv('TMDB_API_KEY', 'should-not-be-used')
    with pytest.raises(ValueError, match='did not provide a TMDB ID'):
        await resolve_tmdb(service, {'id':'anime:1','name':'Similar title'})
    assert fetch.await_count == 1 and fetch.await_args.args[0].startswith('https://metadata.test/')


@pytest.mark.asyncio
async def test_metadata_falls_back_to_capable_enabled_source_preserving_episode(tmp_path, monkeypatch):
    service = Discovery(Store(tmp_path/'db'))
    service.register('https://catalog.test/manifest.json', {'resources':['catalog'], 'types':['series']})
    service.register('https://meta.test/manifest.json', {'resources':[{'name':'meta','types':['series'],'idPrefixes':['tt']}]})
    service.register('https://wrong.test/manifest.json', {'resources':['meta'],'types':['movie']})
    fetch = AsyncMock(return_value={'meta':{'name':'Example','moviedb_id':12,'season':99,'id':'rewritten-id','type':'movie'}})
    monkeypatch.setattr('satoubot.discovery.fetch_json', fetch)
    movie = {'id':'tt1','type':'series','addon':'https://catalog.test/manifest.json','season':2,'episode':10,'video_id':'tt1:2:10'}
    result = await service.details(movie)
    fetch.assert_awaited_once_with('https://meta.test/meta/series/tt1.json')
    assert result['moviedb_id'] == 12 and result['season'] == 2 and result['id'] == 'tt1' and result['type'] == 'series'


@pytest.mark.asyncio
async def test_incompatible_stream_addons_are_never_requested(tmp_path, monkeypatch):
    service = Discovery(Store(tmp_path/'db'))
    service.store.set_setting('source_selection_explicit','1')
    service.register('https://wrong.test/manifest.json', {'types':['series'], 'resources':[{'name':'stream','idPrefixes':['kitsu:']}]})
    fetch = AsyncMock()
    monkeypatch.setattr('satoubot.discovery.fetch_json', fetch)
    assert await service.streams({'id':'tt1','type':'series','video_id':'tt1:1:2','season':1,'episode':2}) == ([], [])
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_embedded_episode_stream_uses_headers_without_querying_other_sources(tmp_path, monkeypatch):
    service = Discovery(Store(tmp_path/'db'))
    fetch = AsyncMock()
    monkeypatch.setattr('satoubot.discovery.fetch_json', fetch)
    movie = {'id':'tt1','type':'series','season':1,'episode':2, 'embedded_streams':[{'url':'https://file.test/episode.mkv','behaviorHints':{'proxyHeaders':{'request':{'Referer':'https://source.test'}}}}]}
    callback = AsyncMock()
    streams, errors = await service.streams(movie, callback)
    assert not errors and streams[0]['headers'] == {'Referer':'https://source.test'}
    callback.assert_awaited_once()
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_plugin_mapping_keeps_addon_partial_results(tmp_path, monkeypatch):
    service = Discovery(Store(tmp_path/'db'))
    service.store.set_setting('source_selection_explicit','1')
    service.register('https://repo.test/manifest.json', {'scrapers':[{'filename':'one.js','name':'One','supportedTypes':['movie']}]})
    service.register('https://addon.test/manifest.json', {'resources':['stream'],'types':['movie']})
    monkeypatch.setattr('satoubot.discovery.fetch_json', AsyncMock(return_value={'streams':[{'url':'https://file.test/movie.mkv'}]}))
    streams, errors = await service.streams({'id':'tt123'})
    assert len(streams) == 1 and any('did not provide a TMDB ID' in error for error in errors)


@pytest.mark.asyncio
async def test_disabled_metadata_source_cannot_supply_cached_embedded_files(tmp_path, monkeypatch):
    service = Discovery(Store(tmp_path/'db'))
    url = 'https://catalog.test/manifest.json'
    service.register(url, {'resources':['meta']})
    service.toggle(url)
    fetch = AsyncMock()
    monkeypatch.setattr('satoubot.discovery.fetch_json', fetch)
    movie = {'id':'tt1','addon':url,'streams':[{'url':'https://file.test/old.mkv'}]}
    assert await service.streams(movie) == ([], [])
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalogue_id_feeds_multiple_tv_plugins_without_api_key(tmp_path, monkeypatch):
    service = Discovery(Store(tmp_path/'db'))
    service.register('https://catalog.test/manifest.json', {'types':['series'], 'resources':['meta'], 'idPrefixes':['tt']})
    service.register('https://repo.test/manifest.json', {'scrapers':[
        {'name':'One', 'filename':'one.js', 'supportedTypes':['tv']},
        {'name':'Two', 'filename':'two.js', 'supportedTypes':['tv']}]})
    service.store.set_setting('source_selection_explicit', '1')
    fetch = AsyncMock(return_value={'meta':{'id':'tt123', 'moviedb_id':1234}})
    monkeypatch.setattr('satoubot.discovery.fetch_json', fetch)
    monkeypatch.setattr('satoubot.discovery.fetch_bytes', AsyncMock(return_value=b'code'))
    run = AsyncMock(side_effect=[[{'url':'https://one.test/file.mkv'}], [{'url':'https://two.test/file.mkv'}]])
    monkeypatch.setattr('satoubot.discovery.run_plugin', run)
    streams, errors = await service.streams({'id':'tt123', 'type':'series', 'season':2, 'episode':10, 'video_id':'tt123:2:10'})
    assert len(streams) == 2 and not errors
    assert fetch.await_count == 1
    assert run.await_count == 2
    assert all(call.args == ('code', '1234', 'tv', 2, 10) for call in run.await_args_list)
