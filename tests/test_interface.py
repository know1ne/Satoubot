import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telegram.error import BadRequest, Forbidden

from satoubot.app import Bot, admin_session
from satoubot.discovery import Discovery
from satoubot.store import Store


def setup_bot(tmp_path):
    bot = Bot(Store(tmp_path/'db'), tmp_path, 42, MagicMock())
    message = MagicMock()
    panel = MagicMock()
    panel.edit_text = AsyncMock()
    panel.delete = AsyncMock()
    panel.edit_reply_markup = AsyncMock()
    message.reply_text = AsyncMock(return_value=panel)
    context = SimpleNamespace(user_data={}, args=[], bot=MagicMock())
    context.bot.send_message = AsyncMock(return_value=panel)
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=42),
                             effective_chat=SimpleNamespace(type='private'), callback_query=None)
    return bot, message, panel, context, update


def callback(update, panel, data):
    update.callback_query = SimpleNamespace(data=data, message=panel, answer=AsyncMock())


@pytest.mark.asyncio
async def test_navigation_edits_one_panel_and_back_restores_prompt(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    await bot.main_menu(message)
    callback(update, panel, 'menu:addons')
    await admin_session(bot.callback)(update, context)
    assert not context.user_data.get('manifest_pending')
    callback(update, panel, 'menu:addmanifest')
    await admin_session(bot.callback)(update, context)
    assert context.user_data['manifest_pending']
    callback(update, panel, 'menu:library')
    await admin_session(bot.callback)(update, context)
    assert 'manifest_pending' not in context.user_data
    callback(update, panel, 'menu:back')
    await admin_session(bot.callback)(update, context)
    assert context.user_data['manifest_pending']
    assert 'search_pending' not in context.user_data
    assert 'manifest URL' in panel.edit_text.await_args.args[0]
    assert message.reply_text.await_count == 1


@pytest.mark.asyncio
async def test_pending_check_follows_navigation_and_duplicate_click_does_not_restart(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    await bot.main_menu(message)
    started, release = asyncio.Event(), asyncio.Event()
    async def work():
        started.set()
        await release.wait()
    factory = MagicMock(side_effect=work)
    await bot.start_panel_task('file', 'Checking Example.mkv', factory, context)
    await started.wait()
    callback(update, panel, 'menu:library')
    await admin_session(bot.callback)(update, context)
    text = panel.edit_text.await_args.args[0]
    assert 'published library' in text and 'Checking Example.mkv' in text
    await bot.start_panel_task('file', 'Checking Example.mkv', factory, context)
    factory.assert_called_once()
    release.set()
    await asyncio.gather(*bot.discovery_tasks)
    assert 'In progress' not in panel.edit_text.await_args.args[0]
    assert 'published library' in panel.edit_text.await_args.args[0]
    assert message.reply_text.await_count == 1


@pytest.mark.asyncio
async def test_search_completion_attaches_results_without_replacing_new_menu(tmp_path, monkeypatch):
    bot, message, panel, context, update = setup_bot(tmp_path)
    Discovery(bot.store).register('https://catalog.test/manifest.json', {'name':'test', 'catalogs':[]})
    started, release = asyncio.Event(), asyncio.Event()
    async def search(self, query):
        started.set()
        await release.wait()
        return [{'id':'tt1','name':'Example','year':'2024'}], []
    monkeypatch.setattr(Discovery, 'search', search)
    await bot.begin_search(message, 'Example')
    await started.wait()
    await bot.main_menu(message)
    release.set()
    await asyncio.gather(*bot.discovery_tasks)
    call = panel.edit_text.await_args
    assert 'Main menu' in call.args[0]
    result = next(b for row in call.kwargs['reply_markup'].inline_keyboard for b in row if b.text == 'Results: Example')
    callback(update, panel, result.callback_data)
    await admin_session(bot.callback)(update, context)
    assert panel.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].text == 'Example (2024)'
    assert message.reply_text.await_count == 1


@pytest.mark.asyncio
async def test_search_completion_loads_buttons_into_current_search_panel(tmp_path, monkeypatch):
    bot, message, panel, context, update = setup_bot(tmp_path)
    Discovery(bot.store).register('https://catalog.test/manifest.json', {'name':'test', 'catalogs':[]})
    monkeypatch.setattr(Discovery, 'search', AsyncMock(return_value=([{'id':'tt1','name':'Example'}], [])))
    await bot.begin_search(message, 'Example')
    await asyncio.gather(*bot.discovery_tasks)
    assert panel.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data.startswith('movie:')
    assert 'In progress' not in panel.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_idle_cleanup_deletes_only_temporary_messages_and_not_active_panel(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    await bot.main_menu(message)
    context.bot.send_message.return_value = MagicMock(delete=AsyncMock())
    durable = await context.bot.send_message(42, 'Download done')
    old_notice = MagicMock(delete=AsyncMock())
    bot.panel().retired.append(old_notice)
    now = bot.panel().updated
    await bot.cleanup_panels(now+299)
    panel.delete.assert_not_awaited()
    bot.panel().pending['file'] = 'Checking source'
    await bot.cleanup_panels(now+600)
    panel.delete.assert_not_awaited()
    bot.panel().pending.clear()
    await bot.cleanup_panels(now+600)
    panel.delete.assert_awaited_once()
    old_notice.delete.assert_awaited_once()
    durable.delete.assert_not_awaited()
    assert bot.panel().message is None
    await bot.main_menu(message)
    assert message.reply_text.await_count == 2


@pytest.mark.asyncio
async def test_cleanup_deletion_denied_removes_controls_without_crashing(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    await bot.main_menu(message)
    panel.delete.side_effect = Forbidden('Cannot delete')
    await bot.cleanup_panels(bot.panel().updated+301)
    panel.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    assert len(bot.panel().retired) == 1


@pytest.mark.asyncio
async def test_deleted_panel_is_replaced_and_not_modified_does_not_duplicate(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    await bot.main_menu(message)
    panel.edit_text.side_effect = BadRequest('Message is not modified')
    await bot.main_menu(message)
    assert message.reply_text.await_count == 1
    panel.edit_text.side_effect = BadRequest('Message to edit not found')
    replacement = MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
    message.reply_text.return_value = replacement
    await bot.main_menu(message)
    assert bot.panel().message is replacement
    assert bot.panel().retired == [panel]


@pytest.mark.asyncio
async def test_two_admins_have_independent_panels_and_inactivity(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    bot.store.db.execute('INSERT INTO admins VALUES (1234)')
    bot.store.db.commit()
    await admin_session(bot.start)(update, context)
    first = bot.panel()
    other_panel = MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
    other_message = MagicMock(reply_text=AsyncMock(return_value=other_panel))
    other = SimpleNamespace(message=other_message, effective_user=SimpleNamespace(id=1234), effective_chat=SimpleNamespace(type='private'))
    await admin_session(bot.start)(other, SimpleNamespace(user_data={}, args=[]))
    assert bot.panels[1234] is not first and bot.panels[1234].message is other_panel
    await bot.cleanup_panels(first.updated+299)
    panel.delete.assert_not_awaited()
    other_panel.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_prompt_expires_but_running_operation_survives(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    context.user_data['manifest_pending'] = True
    bot.panel().updated = time.monotonic() - 301
    bot.touch_panel(context)
    assert 'manifest_pending' not in context.user_data
    context.user_data['manifest_pending'] = True
    bot.panel().updated = time.monotonic() - 301
    bot.panel().pending['movie'] = 'Finding files'
    bot.touch_panel(context)
    assert context.user_data['manifest_pending']


@pytest.mark.asyncio
async def test_migration_entry_saved_pagination_and_pending_list_avoid_message_fanout(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    await bot.main_menu(message)
    assert any(b.text == 'Migration' for row in message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard for b in row)
    callback(update, panel, 'menu:migration')
    await admin_session(bot.callback)(update, context)
    assert 'Migration' in panel.edit_text.await_args.args[0]
    for i in range(12):
        key = bot.store.create(f'https://file.test/{i}', 'Example', f'{i}.mkv', 100)
        bot.store.update(key, state='oversize')
    await bot.saved(update, context)
    rows = panel.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
    assert len(rows) == 10 and rows[-2][0].text == 'More'
    for i in range(4):
        key = bot.store.create(f'https://review.test/{i}', 'Example', f'{i}.mkv', 100)
        bot.store.update(key, state='review')
    bot.preview = AsyncMock()
    await bot.pending(update, context)
    bot.preview.assert_not_awaited()
    assert len(panel.edit_text.await_args.kwargs['reply_markup'].inline_keyboard) == 5
    assert message.reply_text.await_count == 1


@pytest.mark.asyncio
async def test_file_check_starts_while_partial_provider_results_are_loading(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    await bot.main_menu(message)
    started, release = asyncio.Event(), asyncio.Event()
    async def provider():
        started.set()
        await release.wait()
    await bot.start_panel_task('movie', 'Finding files', provider, context)
    await started.wait()
    key = bot.store.cache('stream', {'url':'https://file.test/movie.mkv', 'movie':{'name':'Example'}, 'filename':'movie.mkv', 'size':100})
    bot.prepare_choice = AsyncMock()
    callback(update, panel, f'file:{key}')
    await admin_session(bot.callback)(update, context)
    await asyncio.sleep(0)
    bot.prepare_choice.assert_awaited_once()
    assert 'movie' in bot.panel().pending
    release.set()
    await asyncio.gather(*bot.discovery_tasks)
    assert not bot.panel().pending


@pytest.mark.asyncio
async def test_cancelled_background_check_clears_pending_footer(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    await bot.main_menu(message)
    started = asyncio.Event()
    async def check():
        started.set()
        await asyncio.Event().wait()
    await bot.start_panel_task('file', 'Checking file', check, context)
    await started.wait()
    task = next(iter(bot.discovery_tasks))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not bot.panel().pending
    assert 'In progress' not in panel.edit_text.await_args.args[0]
