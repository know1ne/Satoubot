import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telegram.error import BadRequest, Forbidden

from satoubot.app import Bot, admin_session, latest_command
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
    notice = MagicMock(delete=AsyncMock())
    async def reply(text, **kwargs):
        return notice if text == 'Opening menu…' else panel
    message.reply_text.side_effect = reply
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
    message.reply_text.side_effect = None
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
    other_notice = MagicMock(delete=AsyncMock())
    async def other_reply(text, **kwargs):
        return other_notice if text == 'Opening menu…' else other_panel
    other_message = MagicMock(reply_text=AsyncMock(side_effect=other_reply))
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


@pytest.mark.asyncio
async def test_latest_search_replaces_earlier_search_and_preserves_transfer(tmp_path, monkeypatch):
    bot, message, panel, context, update = setup_bot(tmp_path)
    Discovery(bot.store).register('https://catalog.test/manifest.json', {'name':'test', 'catalogs':[]})
    first_started = asyncio.Event()
    cancelled = asyncio.Event()
    transfer_release = asyncio.Event()
    async def transfer():
        await transfer_release.wait()
    bot.spawn(transfer())
    transfer_task = next(iter(bot.tasks))
    async def search(self, query):
        if query == 'First':
            first_started.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        return [{'id':'second', 'name':query}], []
    monkeypatch.setattr(Discovery, 'search', search)
    await bot.begin_search(message, 'First')
    await first_started.wait()
    await bot.begin_search(message, 'Second')
    await asyncio.gather(*bot.discovery_tasks)
    assert cancelled.is_set()
    assert bot.panel().markup.inline_keyboard[0][0].text.startswith('Second')
    assert not transfer_task.done()
    transfer_release.set()
    await transfer_task


@pytest.mark.asyncio
async def test_latest_command_interrupts_blocked_handler_and_only_its_admin(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    bot.store.db.execute('INSERT INTO admins VALUES (1234)'); bot.store.db.commit()
    started = asyncio.Event()
    stopped = asyncio.Event()
    other_release = asyncio.Event()
    async def slow(update, context):
        if update.effective_user.id == 1234:
            await other_release.wait()
            return
        started.set()
        try: await asyncio.Event().wait()
        finally: stopped.set()
    async def fast(update, context):
        await bot.show_panel(update.message, 'Latest command')
    bot.slow = slow; bot.fast = fast
    # Use bound methods as real command handlers do.
    from types import MethodType
    bot.slow = MethodType(lambda self, update, context: slow(update, context), bot)
    bot.fast = MethodType(lambda self, update, context: fast(update, context), bot)
    other = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1234), effective_chat=SimpleNamespace(type='private'))
    other_task = asyncio.create_task(latest_command(bot.slow)(other, context))
    first = asyncio.create_task(latest_command(bot.slow)(update, context))
    await started.wait()
    await latest_command(bot.fast)(update, context)
    assert stopped.is_set() and first.cancelled()
    assert not other_task.done() and bot.panel().text == 'Latest command'
    other_release.set(); await other_task


@pytest.mark.asyncio
async def test_start_deletes_temporary_opening_notice(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    notice = await message.reply_text('Opening menu…')
    await admin_session(bot.start)(update, context)
    notice.delete.assert_awaited_once()
    assert bot.panel().message is panel


@pytest.mark.asyncio
async def test_new_command_clears_old_prompt_but_text_can_answer_prompt(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    context.user_data['manifest_pending'] = True
    await latest_command(bot.start)(update, context)
    assert 'manifest_pending' not in context.user_data
    context.user_data['catalogue_search_pending'] = True
    message.text = 'One Piece'
    bot.begin_search = AsyncMock()
    await latest_command(bot.text, clear_prompt=False)(update, context)
    bot.begin_search.assert_awaited_once_with(message, 'One Piece')


@pytest.mark.asyncio
async def test_latest_command_cancels_only_own_admins_background_lookup(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    bot.store.db.execute('INSERT INTO admins VALUES (1234)'); bot.store.db.commit()
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    other_release = asyncio.Event()
    async def first_check():
        first_started.set()
        await asyncio.Event().wait()
    async def second_check():
        second_started.set()
        await other_release.wait()
    await bot.start_panel_task('movie', 'First lookup', first_check, context)
    await first_started.wait()
    other = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1234), effective_chat=SimpleNamespace(type='private'), callback_query=None)
    async def start_other(update, context):
        await bot.start_panel_task('search', 'Other lookup', second_check, context)
    await admin_session(start_other)(other, context)
    await second_started.wait()
    await latest_command(bot.start)(update, context)
    assert not bot.panels[42].pending
    assert 'search' in bot.panels[1234].pending
    other_release.set()
    await asyncio.gather(*bot.discovery_tasks)


@pytest.mark.asyncio
async def test_stop_cancels_pending_search_and_keeps_transfer(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    lookup_started = asyncio.Event()
    lookup_cancelled = asyncio.Event()
    transfer_release = asyncio.Event()
    async def lookup():
        lookup_started.set()
        try: await asyncio.Event().wait()
        finally: lookup_cancelled.set()
    async def transfer():
        await transfer_release.wait()
    bot.spawn(transfer())
    transfer_task = next(iter(bot.tasks))
    await bot.start_panel_task('search', 'Searching', lookup, context)
    await lookup_started.wait()
    await latest_command(bot.stop)(update, context)
    assert lookup_cancelled.is_set() and not bot.panel().pending
    assert 'Stopped your pending command or lookup.' in bot.panel().text
    assert 'Downloads and uploads continue.' in bot.panel().text
    assert not transfer_task.done()
    transfer_release.set(); await transfer_task


@pytest.mark.asyncio
async def test_stop_cancels_active_handler_and_reports_idle_on_repeat(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    from types import MethodType
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def slow(self, update, context):
        started.set()
        try: await asyncio.Event().wait()
        finally: cancelled.set()
    bot.slow = MethodType(slow, bot)
    running = asyncio.create_task(latest_command(bot.slow)(update, context))
    await started.wait()
    await latest_command(bot.stop)(update, context)
    assert running.cancelled() and cancelled.is_set()
    assert bot.panel().text == 'Stopped your pending command or lookup.'
    await latest_command(bot.stop)(update, context)
    assert bot.panel().text == 'No command or lookup is running.'


@pytest.mark.asyncio
async def test_stop_does_not_cancel_another_admins_lookup(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    bot.store.db.execute('INSERT INTO admins VALUES (1234)'); bot.store.db.commit()
    started, release = asyncio.Event(), asyncio.Event()
    async def lookup():
        started.set(); await release.wait()
    other = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1234), effective_chat=SimpleNamespace(type='private'), callback_query=None)
    async def start_other(update, context):
        await bot.start_panel_task('search', 'Other lookup', lookup, context)
    await admin_session(start_other)(other, context)
    await started.wait()
    await latest_command(bot.stop)(update, context)
    assert bot.panel().text == 'No command or lookup is running.'
    assert 'search' in bot.panels[1234].pending
    release.set(); await asyncio.gather(*bot.discovery_tasks)
