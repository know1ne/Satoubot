import asyncio
import socket
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from satoubot.discovery import run_plugin
from satoubot.plugin_network import fetch_public, public_address


@pytest.mark.asyncio
async def test_host_capability_escape_cannot_reach_local_service():
    reached = asyncio.Event()
    async def handle(reader, writer):
        reached.set()
        writer.close()
    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    code = '''module.exports.getStreams = async () => {
      const proc = fetch.constructor('return process')();
      const net = proc.getBuiltinModule('node:net');
      const answer = await new Promise(resolve => {
        let s;
        try { s = net.connect(PORT, '127.0.0.1'); } catch { resolve('blocked'); return; }
        s.on('connect', () => { s.destroy(); resolve('connected'); });
        s.on('error', () => resolve('blocked'));
        s.setTimeout(1000, () => {s.destroy(); resolve('blocked')});
      });
      return [{url: answer}];
    }'''.replace('PORT', str(port))
    try:
        assert await run_plugin(code, '1') == [{'url':'blocked'}]
        assert not reached.is_set()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_provider_broker_denies_local_urls_and_mixed_dns(monkeypatch):
    with pytest.raises(ValueError):
        await fetch_public('http://127.0.0.1:8081', headers={})
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, 'getaddrinfo', AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 0)),
    ]))
    with pytest.raises(ValueError):
        await public_address('rebinding.test')


@pytest.mark.asyncio
async def test_redirect_is_revalidated_and_address_is_pinned(monkeypatch):
    import satoubot.plugin_network as broker
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={'location':'http://127.0.0.1/secret'})
    original = httpx.AsyncClient
    monkeypatch.setattr(broker.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    resolver = AsyncMock(side_effect=['8.8.8.8', ValueError('private')])
    monkeypatch.setattr(broker, 'public_address', resolver)
    with pytest.raises(ValueError):
        await fetch_public('https://public.test/path', headers={'Host':'127.0.0.1'})
    assert resolver.await_count == 2
    assert len(requests) == 1
    assert requests[0].url.host == '8.8.8.8'
    assert requests[0].headers['host'] == 'public.test'
    assert requests[0].extensions['sni_hostname'] == 'public.test'


@pytest.mark.asyncio
async def test_plugin_fails_closed_without_bubblewrap(monkeypatch):
    import satoubot.discovery as discovery
    which = discovery.shutil.which
    monkeypatch.setattr(discovery.shutil, 'which', lambda name: None if name == 'bwrap' else which(name))
    with pytest.raises(RuntimeError, match='bubblewrap'):
        await run_plugin('module.exports.getStreams=async()=>[]', '1')


@pytest.mark.asyncio
async def test_host_files_and_environment_are_not_exposed(tmp_path, monkeypatch):
    secret = tmp_path / 'secret'
    secret.write_text('private')
    monkeypatch.setenv('SATOU_SECRET_TEST', 'private')
    code = '''module.exports.getStreams=async()=>{
      const proc=fetch.constructor('return process')();
      let found=false;
      try {found=proc.getBuiltinModule('node:fs').readFileSync(PATH,'utf8') === 'private'} catch {}
      return [{url: String(found || proc.env.SATOU_SECRET_TEST === 'private')}];
    }'''.replace('PATH', repr(str(secret)))
    assert await run_plugin(code, '1') == [{'url':'false'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('address', ['::1', '::ffff:127.0.0.1', '64:ff9b::7f00:1', '2002:7f00:1::'])
async def test_embedded_private_ipv6_addresses_rejected(monkeypatch, address):
    loop=asyncio.get_running_loop()
    monkeypatch.setattr(loop, 'getaddrinfo', AsyncMock(return_value=[
        (socket.AF_INET6, socket.SOCK_STREAM, 6, '', (address, 0, 0, 0))
    ]))
    with pytest.raises(ValueError):
        await public_address('mapped.test')


def test_node_version_check_and_cache(monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from satoubot.discovery import ProviderRuntimeError, node_major
    node_major.cache_clear()
    run = MagicMock(return_value=SimpleNamespace(stdout='v24.21.0\n'))
    monkeypatch.setattr(subprocess, 'run', run)
    assert node_major('/node24') == node_major('/node24') == 24
    run.assert_called_once()
    run.return_value.stdout='v25.9.0\n'
    assert node_major('/node25') == 25
    run.return_value.stdout='v22.0.0\n'
    with pytest.raises(ProviderRuntimeError, match='Node.js 24 or newer'):
        node_major('/node22')
    run.return_value.stdout='invalid\n'
    with pytest.raises(ProviderRuntimeError, match='could not determine'):
        node_major('/nodeinvalid')
    node_major.cache_clear()
