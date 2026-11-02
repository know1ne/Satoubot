import asyncio
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon import functions, types, utils

from satoubot.app import Bot
from satoubot.parallel_upload import PART_SIZE, upload_parts
from satoubot.store import Store


class FakeTelegram:
    def __init__(self, delay=0):
        self.parts = {}
        self.active = self.peak = 0
        self.delay = delay
        self.send_file = AsyncMock(return_value=MagicMock(id=99))

    async def __call__(self, request):
        assert isinstance(request, functions.upload.SaveBigFilePartRequest)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay * (4 if request.file_part % 4 == 0 else 1))
            self.parts[request.file_part] = request.bytes
            return True
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_parallel_parts_are_bounded_complete_and_correct(tmp_path):
    original=bytes(range(256)) * (11 * 1024 * 1024 // 256 + 17)
    path=tmp_path/'Movie.mkv'; path.write_bytes(original)
    client=FakeTelegram(.001); updates=[]
    async def progress(done,total): updates.append((done,total))
    handle=await upload_parts(client,path,len(original),4,progress)
    assert isinstance(handle,types.InputFileBig)
    assert 1 < client.peak <= 4
    assert handle.name == 'Movie.mkv'
    assert len(client.parts)==handle.parts
    reconstructed=b''.join(client.parts[i] for i in range(handle.parts))
    assert hashlib.sha256(reconstructed).digest()==hashlib.sha256(original).digest()
    assert all(len(data)==PART_SIZE for i,data in client.parts.items() if i < handle.parts-1)
    assert updates == sorted(updates) and updates[-1] == (len(original),len(original))


@pytest.mark.asyncio
async def test_worker_failure_and_cancellation_stop_other_parts(tmp_path):
    path=tmp_path/'Movie.mkv';path.write_bytes(b'0'*(11*1024*1024))
    entered=asyncio.Event(); release=asyncio.Event()
    class Broken(FakeTelegram):
        async def __call__(self,request):
            if request.file_part==0:
                await entered.wait()
                return False
            entered.set()
            await release.wait()
            return True
    client=Broken()
    with pytest.raises(ExceptionGroup):
        await asyncio.wait_for(upload_parts(client,path,path.stat().st_size,4,AsyncMock()),1)
    client.send_file.assert_not_awaited()
    assert entered.is_set()
    class Slow(FakeTelegram):
        async def __call__(self,request):
            entered.set()
            await release.wait()
            return True
    entered.clear();task=asyncio.create_task(upload_parts(Slow(),path,path.stat().st_size,4,AsyncMock()))
    await asyncio.wait_for(entered.wait(),1);task.cancel()
    with pytest.raises(asyncio.CancelledError): await task


@pytest.mark.asyncio
async def test_parallel_upload_publishes_only_after_all_parts_and_preserves_attributes(tmp_path):
    path=tmp_path/'Movie.mkv';path.write_bytes(b'0'*(11*1024*1024))
    store=Store(tmp_path/'db');job=store.create('https://file','Movie','Movie.mkv',path.stat().st_size)
    thumb=tmp_path/'thumb.jpg';thumb.write_bytes(b'thumbnail')
    store.update(job,state='uploading',target='-100123',path=str(path),thumbnail=str(thumb))
    client=FakeTelegram(.001)
    async def send(target,uploaded,**kwargs):
        assert len(client.parts)==uploaded.parts
        assert uploaded.name=='Movie.mkv' and target==-100123
        assert kwargs['caption']=='Movie' and kwargs['thumb']==str(thumb)
        assert any(getattr(item,'file_name',None)=='Movie.mkv' for item in kwargs['attributes'])
        assert kwargs['mime_type']==utils.get_attributes(str(path))[1]
        return MagicMock(id=99)
    client.send_file=AsyncMock(side_effect=send)
    bot=Bot(store,tmp_path,42,client,upload_parallel_parts=4)
    context=MagicMock();context.bot.send_message=AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    await bot.perform_upload(context,job)
    assert store.get(job)['state']=='published'
    assert store.get(job)['telegram_message_id']==99


@pytest.mark.asyncio
async def test_bad_file_size_and_failed_parts_never_publish(tmp_path):
    path=tmp_path/'Movie.mkv';path.write_bytes(b'0'*(11*1024*1024))
    store=Store(tmp_path/'db');job=store.create('https://file','Movie','Movie.mkv',path.stat().st_size+1)
    store.update(job,state='uploading',target='-100123',path=str(path))
    client=FakeTelegram()
    bot=Bot(store,tmp_path,42,client,upload_parallel_parts=4)
    context=MagicMock();context.bot.send_message=AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    await bot.perform_upload(context,job)
    assert store.get(job)['state']=='upload_unknown'
    client.send_file.assert_not_awaited()


def test_invalid_worker_configuration_is_rejected(tmp_path):
    for count in (0,5):
        with pytest.raises(ValueError,match='UPLOAD_PARALLEL_PARTS'):
            Bot(Store(tmp_path/f'db{count}'),tmp_path,42,MagicMock(),upload_parallel_parts=count)


@pytest.mark.asyncio
async def test_failed_part_does_not_publish(tmp_path):
    path=tmp_path/'Movie.mkv';path.write_bytes(b'0'*(11*1024*1024))
    store=Store(tmp_path/'db');job=store.create('https://file','Movie','Movie.mkv',path.stat().st_size)
    store.update(job,state='uploading',target='-100123',path=str(path))
    class Failure(FakeTelegram):
        async def __call__(self,request): return False
    client=Failure();bot=Bot(store,tmp_path,42,client,upload_parallel_parts=4)
    context=MagicMock();context.bot.send_message=AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    await bot.perform_upload(context,job)
    assert store.get(job)['state']=='upload_unknown'
    client.send_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_parallel_handle_works_with_telethon_media_conversion(tmp_path):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    path=tmp_path/'Movie.mkv';path.write_bytes(b'not-a-real-video')
    attrs,mime=utils.get_attributes(str(path))
    client=TelegramClient(StringSession(),1,'synthetic')
    handle=types.InputFileBig(12,22,path.name)
    _,media,_=await client._file_to_media(handle,attributes=attrs,mime_type=mime)
    assert isinstance(media,types.InputMediaUploadedDocument)
    assert media.file==handle and media.mime_type==mime
    assert any(isinstance(item,types.DocumentAttributeFilename) and item.file_name=='Movie.mkv' for item in media.attributes)


@pytest.mark.asyncio
async def test_cancelling_parallel_upload_never_publishes(tmp_path):
    path=tmp_path/'Movie.mkv';path.write_bytes(b'0'*(11*1024*1024))
    store=Store(tmp_path/'db');job=store.create('https://file','Movie','Movie.mkv',path.stat().st_size)
    store.update(job,state='uploading',target='-100123',path=str(path))
    entered=asyncio.Event()
    class Slow(FakeTelegram):
        async def __call__(self,request):
            entered.set();await asyncio.Event().wait()
    client=Slow();bot=Bot(store,tmp_path,42,client,upload_parallel_parts=4)
    context=MagicMock();context.bot.send_message=AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    task=asyncio.create_task(bot.perform_upload(context,job))
    await asyncio.wait_for(entered.wait(),1);task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert store.get(job)['state']=='upload_unknown'
    client.send_file.assert_not_awaited()
