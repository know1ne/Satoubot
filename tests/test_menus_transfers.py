import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import ReplyKeyboardRemove
from satoubot.app import Bot, admin_session
from satoubot.discovery import Discovery
from satoubot.store import Store


def update_for(uid=42):
    update=MagicMock();update.effective_user.id=uid;update.effective_chat.type='private'
    update.message.reply_text=AsyncMock()
    return update


@pytest.mark.asyncio
async def test_inline_menu_removes_keyboard_and_has_no_search_button(tmp_path):
    bot=Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock())
    update=update_for();context=MagicMock();context.args=[]
    await bot.start(update,context)
    assert isinstance(update.message.reply_text.await_args_list[0].kwargs['reply_markup'],ReplyKeyboardRemove)
    buttons=update.message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard
    names=[b.text for row in buttons for b in row]
    assert 'Search' not in names and 'Active transfers' in names and 'Admin management' in names


@pytest.mark.asyncio
async def test_owner_add_remove_admin_persists_and_admin_cannot_manage(tmp_path):
    path=tmp_path/'db';store=Store(path);bot=Bot(store,tmp_path,42,MagicMock())
    context=MagicMock();context.user_data={}
    update=update_for();q=MagicMock();q.data='menu:addadmin';q.message.reply_text=AsyncMock();update.callback_query=q
    await bot.menu_callback(update,context)
    update.message.text='1234';await bot.text(update,context)
    assert Bot(Store(path),tmp_path,42,MagicMock()).is_admin(1234)
    outsider=update_for(1234);outsider.callback_query=q
    q.data='adminremove:1234';await bot.menu_callback(outsider,context)
    assert bot.is_admin(1234)
    q.data='adminremove:1234';await bot.menu_callback(update,context)
    assert not bot.is_admin(1234)


@pytest.mark.asyncio
async def test_admin_context_routes_task_messages_to_correct_admin(tmp_path):
    bot=Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock())
    bot.store.db.execute('INSERT INTO admins VALUES (1234)');bot.store.db.commit()
    received=[]
    async def handler(update,context):
        assert bot.authorized(update)
        async def work(): received.append(bot.admin_id)
        bot.spawn(work())
    await admin_session(handler)(update_for(1234),MagicMock())
    await asyncio.gather(*bot.tasks)
    assert received==[1234] and bot.admin_id==42


@pytest.mark.asyncio
async def test_cancel_upload_marks_unknown_and_prevents_retries(tmp_path):
    store=Store(tmp_path/'db');job=store.create('https://file','Example','test.mkv',100)
    store.update(job,state='uploading',target='-100123',path=str(tmp_path/'file'))
    started=asyncio.Event()
    async def upload(*args,**kwargs):
        started.set();await asyncio.Event().wait()
    uploader=MagicMock();uploader.send_file=upload
    bot=Bot(store,tmp_path,42,uploader)
    context=MagicMock();context.bot.send_message=AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    bot.spawn(bot.perform_upload(context,job));await started.wait()
    task=next(iter(bot.tasks));task.cancel()
    await asyncio.gather(task,return_exceptions=True)
    assert store.get(job)['state']=='upload_unknown'


@pytest.mark.asyncio
async def test_rename_with_extension_only_once_and_upload_rejects_rename(tmp_path):
    store=Store(tmp_path/'db');job=store.create('https://file','Example','old.mkv',100)
    file=tmp_path/'old.mkv';file.write_bytes(b'x');store.update(job,state='review',path=str(file))
    bot=Bot(store,tmp_path,42,MagicMock());bot.preview=AsyncMock()
    update=update_for();context=MagicMock();context.args=[str(job),'New','movie.mkv']
    await bot.rename(update,context)
    assert store.get(job)['filename']=='New movie.mkv'
    store.update(job,state='uploading')
    context.args=[str(job),'Other.mkv'];await bot.rename(update,context)
    assert store.get(job)['filename']=='New movie.mkv'


@pytest.mark.asyncio
async def test_exact_title_ranks_first(tmp_path,monkeypatch):
    store=Store(tmp_path/'db');service=Discovery(store)
    service.register('https://catalog.test/manifest.json',{'name':'Catalogue','catalogs':[{'id':'x','type':'movie','extra':[{'name':'search'}]}]})
    monkeypatch.setattr('satoubot.discovery.fetch_json',AsyncMock(return_value={'metas':[{'id':'2','name':'Insidious 2'},{'id':'1','name':'Insidious'},{'id':'3','name':'Unrelated'}]}))
    movies,_=await service.search('Insidious')
    assert [m['id'] for m in movies]==['1','2']


@pytest.mark.asyncio
async def test_upload_progress_updates_one_message_and_completion_links_destination(tmp_path,monkeypatch):
    store=Store(tmp_path/'db');job=store.create('https://file','Example','test.mkv',100)
    store.update(job,state='uploading',target='-100123',path=str(tmp_path/'file'))
    event=asyncio.Event();sleep=asyncio.sleep
    async def fast_sleep(seconds):
        assert seconds==5
        await sleep(.001)
    monkeypatch.setattr('satoubot.upload.asyncio.sleep',fast_sleep)
    async def edit(text):
        if 'Transferring' in text:
            assert 'test.mkv' in text and '50 / 100 bytes' in text and 'Speed:' in text and 'ETA:' in text and '█' in text
            event.set()
    message=MagicMock();message.edit_text=AsyncMock(side_effect=edit)
    context=MagicMock();context.bot.send_message=AsyncMock(return_value=message)
    uploader=MagicMock()
    async def upload(*args,progress_callback,**kwargs):
        await progress_callback(50,100)
        await asyncio.wait_for(event.wait(),1)
        return MagicMock(id=99)
    uploader.send_file=upload
    await Bot(store,tmp_path,42,uploader).perform_upload(context,job)
    assert store.get(job)['state']=='published'
    sent=context.bot.send_message.await_args_list
    assert len(sent)==2 and 'Completed' in sent[-1].args[1]
    assert sent[-1].kwargs['reply_markup'].inline_keyboard[0][0].url=='https://t.me/c/123/99'


@pytest.mark.asyncio
async def test_download_recheck_changed_source_never_downloads(tmp_path,monkeypatch):
    store=Store(tmp_path/'db');job=store.create('https://file','Example','test.mkv',100)
    store.update(job,state='downloading')
    bot=Bot(store,tmp_path,42,MagicMock())
    context=MagicMock();context.bot.send_message=AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    monkeypatch.setattr('satoubot.download.probe',AsyncMock(return_value={'status':'unavailable','reason':'File not found'}))
    transfer=AsyncMock();monkeypatch.setattr('satoubot.download.download',transfer)
    await bot.perform_download(context,job)
    transfer.assert_not_awaited()
    assert store.get(job)['state']=='failed'
    assert 'File not found' in context.bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_direct_link_temporary_failure_offers_retry_without_saving(tmp_path,monkeypatch):
    store=Store(tmp_path/'db');bot=Bot(store,tmp_path,42,MagicMock())
    update=update_for();update.message.text='https://public.test/movie.mkv'
    monkeypatch.setattr('satoubot.download.probe',AsyncMock(return_value={'status':'temporary','reason':'Rate limited','retry_after':'15'}))
    await bot.handle_source(update,update.message.text)
    call=update.message.reply_text.await_args
    assert '15' in call.args[0] and call.kwargs['reply_markup'].inline_keyboard[0][0].text=='Retry check'
    assert store.db.execute('SELECT count(*) FROM jobs').fetchone()[0]==0


@pytest.mark.asyncio
async def test_verified_filename_extension_is_shown(tmp_path,monkeypatch):
    import io
    store=Store(tmp_path/'db');bot=Bot(store,tmp_path,42,MagicMock())
    movie={'id':'tt1','name':'Example','year':'2024','background':'https://img.test/image'}
    key=store.cache('movie',movie)
    monkeypatch.setattr(Discovery,'details',AsyncMock(return_value=movie))
    monkeypatch.setattr(Discovery,'streams',AsyncMock(return_value=([{'url':'https://source.test/download','quality':'720p'}],[])))
    monkeypatch.setattr('satoubot.catalogue.probe',AsyncMock(return_value={'status':'ready','size':100,'filename':'file.mkv','content_type':'application/octet-stream'}))
    monkeypatch.setattr('satoubot.catalogue.landscape',AsyncMock(return_value=io.BytesIO(b'jpeg')))
    context=MagicMock();context.bot.send_photo=AsyncMock()
    await bot.movie_card(context,key)
    assert 'mkv' in context.bot.send_photo.await_args.kwargs['reply_markup'].inline_keyboard[0][0].text
