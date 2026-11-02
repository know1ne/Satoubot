from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from satoubot.store import Store
from satoubot.transfer import MAX_BYTES, download, safe_name
from satoubot.app import Bot


def test_store_duplicate_and_published_search(tmp_path):
    store = Store(tmp_path / 'db')
    job = store.create('https://test/file', 'Example', 'example.mp4', 23)
    assert store.find_source('https://test/file')['id'] == job
    assert not store.search('Example')
    store.update(job, state='published', telegram_message_id=42)
    assert store.search('Example')[0]['telegram_message_id'] == 42


def test_safe_name_blocks_path_traversal():
    assert '/' not in safe_name('../../test')
    assert '\\' not in safe_name('..\\test')


@pytest.mark.asyncio
async def test_reject_oversize_without_request(tmp_path):
    with pytest.raises(ValueError):
        await download('https://example.org/file', tmp_path / 'file', MAX_BYTES + 1, AsyncMock())
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_stale_approval_never_uploads(tmp_path):
    store = Store(tmp_path / 'db')
    job = store.create('https://test/file', 'Example', 'example.mp4', 23)
    store.set_setting('target', '-100123')
    store.update(job, state='review', target='-100123', approval_token='newtoken')
    bot = Bot(store, tmp_path, 42, MagicMock())
    q = MagicMock(); q.data = f'upload:{job}:oldtoken'; q.answer = AsyncMock(); q.message.reply_text = AsyncMock()
    update = MagicMock(); update.effective_user.id = 42; update.effective_chat.type = 'private'; update.callback_query = q
    await bot.callback(update, MagicMock())
    assert store.get(job)['state'] == 'review'
    assert not bot.tasks
    q.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_unauthorized_approval_is_ignored(tmp_path):
    store = Store(tmp_path / 'db')
    job = store.create('https://test/file', 'Example', 'example.mp4', 23)
    store.update(job, state='review', approval_token='token', target='-100123')
    bot = Bot(store, tmp_path, 42, MagicMock())
    update = MagicMock(); update.effective_user.id = 43; update.effective_chat.type = 'private'
    update.callback_query.data = f'upload:{job}:token'
    await bot.callback(update, MagicMock())
    assert store.get(job)['state'] == 'review'
    assert not bot.tasks


@pytest.mark.asyncio
async def test_parallel_download_and_range_validation(tmp_path):
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    data = bytes(range(256)) * 16385
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_HEAD(self):
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('ETag', '"test-version"')
            self.end_headers()
        def do_GET(self):
            start, end = map(int, self.headers['Range'].removeprefix('bytes=').split('-'))
            requests.append((start, end, self.headers.get('If-Range')))
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
            self.send_header('Content-Length', str(end-start+1))
            self.end_headers()
            self.wfile.write(data[start:end+1])
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        path = tmp_path/'out'
        size = await download(f'http://127.0.0.1:{server.server_port}/file', path, len(data), AsyncMock())
        assert size == len(data)
        assert path.read_bytes() == data
        assert len(requests) == 4
        assert all(validator == '"test-version"' for _, _, validator in requests)
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join()


@pytest.mark.asyncio
async def test_approved_upload_uses_selected_target_and_marks_published(tmp_path):
    store = Store(tmp_path / 'db')
    job = store.create('https://test/file', 'Example', 'example.mp4', 23)
    file = tmp_path/'example.mp4'; file.write_bytes(b'example')
    store.set_setting('target', '-100123')
    store.update(job, state='review', target='-100123', approval_token='correct', path=str(file))
    uploader = MagicMock(); uploader.send_file = AsyncMock(return_value=MagicMock(id=99))
    bot = Bot(store, tmp_path, 42, uploader)
    context = MagicMock(); context.bot.send_message = AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    q = MagicMock(); q.data = f'upload:{job}:correct'; q.answer = AsyncMock(); q.edit_message_reply_markup = AsyncMock()
    update = MagicMock(); update.effective_user.id = 42; update.effective_chat.type = 'private'; update.callback_query = q
    await bot.callback(update, context)
    import asyncio
    await asyncio.gather(*bot.tasks)
    assert store.get(job)['state'] == 'published'
    assert store.get(job)['telegram_message_id'] == 99
    assert uploader.send_file.await_args.args[:2] == (-100123, str(file))


@pytest.mark.asyncio
async def test_upload_failure_is_not_automatically_retried(tmp_path):
    store = Store(tmp_path / 'db')
    job = store.create('https://test/file', 'Example', 'example.mp4', 23)
    store.update(job, state='uploading', target='-100123', path=str(tmp_path/'file'))
    uploader = MagicMock(); uploader.send_file = AsyncMock(side_effect=ConnectionError())
    bot = Bot(store, tmp_path, 42, uploader)
    context = MagicMock(); context.bot.send_message = AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    await bot.perform_upload(context, job)
    assert store.get(job)['state'] == 'upload_unknown'
    uploader.send_file.assert_awaited_once()
