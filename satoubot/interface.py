"""Temporary menu panels. Durable movie and transfer messages never enter this registry."""
import asyncio
import time
from dataclasses import dataclass, field

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telegram.error import BadRequest, TelegramError


@dataclass
class Panel:
    message: object = None
    text: str = ''
    markup: object = None
    updated: float = field(default_factory=time.monotonic)
    pending: dict = field(default_factory=dict)
    results: dict = field(default_factory=dict)
    retired: list = field(default_factory=list)
    history: list = field(default_factory=list)
    prompt: dict = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class InterfaceFunctions:
    def panel(self):
        if not hasattr(self, 'panels'):
            self.panels = {}
        return self.panels.setdefault(self.admin_id, Panel())

    def touch_panel(self, context):
        self.last_user_activity = time.monotonic()
        task = getattr(self, 'idle_check_task', None)
        if task and not task.done(): task.cancel()
        panel = self.panel()
        if time.monotonic() - panel.updated >= 300 and not panel.pending:
            self.clear_prompt(context)
        panel.updated = time.monotonic()
        keys = ('manifest_pending', 'uploaded_manifest', 'catalogue_search_pending',
                'search_pending', 'admin_add_pending', 'thumbnail_request')
        panel.prompt = {key: context.user_data[key] for key in keys if key in context.user_data}

    async def show_panel(self, message, text, reply_markup=None, *, context=None, remember=True):
        if reply_markup is None:
            from .menu import navigation
            reply_markup = Markup([navigation()])
        panel = self.panel()
        async with panel.lock:
            if remember and panel.text and (panel.text, panel.markup) != (text, reply_markup):
                panel.history.append((panel.text, panel.markup, panel.prompt.copy()))
                panel.history = panel.history[-10:]
            panel.text, panel.markup = text, reply_markup
            await self._render_panel(panel, message, context)

    async def panel_back(self, message, context):
        panel = self.panel()
        if not panel.history:
            return await self.main_menu(message)
        text, markup, prompt = panel.history.pop()
        context.user_data.update(prompt)
        await self.show_panel(message, text, markup, remember=False)

    async def panel_status(self, context, key, label=None):
        panel = self.panel()
        async with panel.lock:
            if label:
                panel.pending[key] = label
            else:
                panel.pending.pop(key, None)
            if panel.message is not None:
                try:
                    await self._render_panel(panel, context=context)
                except TelegramError:
                    pass

    async def _render_panel(self, panel, message=None, context=None):
        text = panel.text
        if panel.pending:
            text += '\n\nIn progress:\n' + '\n'.join('• ' + value for value in panel.pending.values())
        rows = list(panel.markup.inline_keyboard) if panel.markup else []
        for key, title in panel.results.items():
            rows.append([Button(title, callback_data=f'menu:results:{key}')])
        if panel.pending and not any(b.callback_data == 'menu:main' for row in rows for b in row):
            rows.append([Button('Main menu', callback_data='menu:main')])
        markup = Markup(rows) if rows else None
        if panel.message is not None:
            try:
                await panel.message.edit_text(text, reply_markup=markup)
                return
            except BadRequest as exc:
                if 'message is not modified' in str(exc).lower():
                    return
            except TelegramError:
                # Keep the old reference for a later best-effort cleanup.
                pass
            panel.retired.append(panel.message)
            panel.message = None
        if message is not None:
            panel.message = await message.reply_text(text, reply_markup=markup)
        elif context is not None:
            panel.message = await context.bot.send_message(self.admin_id, text, reply_markup=markup)

    async def panel_task(self, key, factory, context):
        """Expose work beneath whichever menu is currently open, without blocking navigation."""
        panel = self.panel()
        try:
            if key == 'movie':
                # Preserve one provider lookup across admins while menus and link checks run.
                if not hasattr(self, 'provider_lookup_lock'):
                    self.provider_lookup_lock = asyncio.Lock()
                async with self.provider_lookup_lock:
                    await factory()
            else:
                await factory()
        finally:
            async with panel.lock:
                panel.pending.pop(key, None)
                if panel.message is not None:
                    try:
                        await self._render_panel(panel, context=context)
                    except TelegramError:
                        pass

    async def start_panel_task(self, key, label, factory, context):
        panel = self.panel()
        if key in panel.pending:
            async with panel.lock:
                await self._render_panel(panel, context=context)
            return
        panel.pending[key] = label
        if not panel.text:
            panel.text = 'Choose another action while this request finishes.'
        try:
            async with panel.lock:
                await self._render_panel(panel, context=context)
        except Exception:
            panel.pending.pop(key, None)
            raise
        self.launch_discovery(self.panel_task(key, factory, context))

    async def cleanup_panels(self, now=None):
        now = time.monotonic() if now is None else now
        for panel in list(getattr(self, 'panels', {}).values()):
            async with panel.lock:
                if now - panel.updated < 300 or panel.pending:
                    continue
                remaining = []
                for message in panel.retired + ([panel.message] if panel.message is not None else []):
                    try:
                        await message.delete()
                    except TelegramError:
                        # Telegram can deny deletion; at least deactivate the old menu.
                        try:
                            await message.edit_reply_markup(reply_markup=None)
                        except TelegramError:
                            pass
                        remaining.append(message)
                panel.message = None
                panel.retired = remaining[-2:]
                panel.text, panel.markup = '', None
                panel.results.clear()
                panel.history.clear()

    async def panel_cleaner(self):
        while True:
            await asyncio.sleep(15)
            await self.cleanup_panels()
