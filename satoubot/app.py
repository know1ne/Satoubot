import asyncio
import logging
import os
import secrets
import time
from pathlib import Path

import psutil
from dotenv import load_dotenv
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                      KeyboardButtonRequestChat, ReplyKeyboardMarkup, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          MessageHandler, filters)
from telethon import TelegramClient

from .store import Store
from .transfer import MAX_BYTES, download, filename_from_url, inspect_url, safe_name

log = logging.getLogger('satoubot')


class Bot:
    def __init__(self, store, media_dir, admin_id, uploader):
        self.store, self.media_dir, self.admin_id, self.uploader = store, media_dir, admin_id, uploader
        self.tasks = set()

    def authorized(self, update):
        return bool(update.effective_user and update.effective_user.id == self.admin_id
                    and update.effective_chat and update.effective_chat.type == 'private')

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def start(self, update, context):
        if not self.authorized(update): return
        keyboard = [[KeyboardButton('Select channel', request_chat=KeyboardButtonRequestChat(request_id=1, chat_is_channel=True, request_title=True))],
                    [KeyboardButton('Select group', request_chat=KeyboardButtonRequestChat(request_id=2, chat_is_channel=False, request_title=True))],
                    [KeyboardButton('Status'), KeyboardButton('Help')]]
        await update.message.reply_text('Choose a destination, then send a direct media URL. Every upload requires approval.', reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

    async def status(self, update, context):
        if not self.authorized(update): return
        process = psutil.Process()
        memory = process.memory_info().rss / (1024 * 1024)
        cpu = psutil.cpu_percent(interval=None)
        target = self.store.setting('target', 'not selected')
        await update.message.reply_text(f'Destination: {target}\nSystem CPU: {cpu:.1f}%\nBot memory: {memory:.1f} MiB\nActive transfers: {len(self.tasks)}')

    async def chat_shared(self, update, context):
        if not self.authorized(update): return
        chat = update.message.chat_shared
        self.store.set_setting('target', chat.chat_id)
        await update.message.reply_text(f'Destination selected: {chat.title or chat.chat_id}. Ensure the uploading account can post there and the bot has the required admin access.')

    async def text(self, update, context):
        if not self.authorized(update): return
        text = update.message.text.strip()
        if text == 'Status': return await self.status(update, context)
        if text == 'Help': return await self.start(update, context)
        if not text.startswith(('https://', 'http://')):
            await update.message.reply_text('Send a direct media URL, or use /find <title> to search published items.')
            return
        old = self.store.find_source(text)
        if old:
            await update.message.reply_text(f'This link already has item #{old["id"]} ({old["state"]}).')
            return
        try:
            size, content_type = await inspect_url(text)
        except Exception as exc:
            log.warning('Source inspection failed: %s', type(exc).__name__)
            await update.message.reply_text('Could not determine a reliable file size. No download was started.')
            return
        if size > MAX_BYTES:
            name = filename_from_url(text)
            job = self.store.create(text, Path(name).stem, name, size)
            self.store.update(job, state='oversize')
            buttons = InlineKeyboardMarkup([[InlineKeyboardButton('Report dead link', callback_data=f'report:{job}')]])
            await update.message.reply_text(f'Saved large file #{job}: {name} ({size / 1_000_000:.1f} MB). Use /saved to see the link.', reply_markup=buttons)
            return
        if content_type.lower().startswith('text/html'):
            await update.message.reply_text('This appears to be a web page, not a direct file. Page link extraction is not yet supported.')
            return
        name = filename_from_url(text)
        job = self.store.create(text, Path(name).stem, name, size)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Download', callback_data=f'dl:{job}')]])
        await update.message.reply_text(f'Item #{job}\nName: {name}\nEstimated size: {size / 1_000_000:.1f} MB\nDownload this file?', reply_markup=keyboard)

    async def callback(self, update, context):
        if not self.authorized(update): return
        q = update.callback_query
        await q.answer()
        try:
            parts = q.data.split(':')
            action, job_id = parts[0], int(parts[1])
            job = self.store.get(job_id)
        except (ValueError, AttributeError): return
        if not job: return
        if action == 'report' and job['state'] in ('oversize', 'reported'):
            self.store.update(job_id, state='reported')
            await q.message.reply_text('Link reported and saved. Automatic source searches are not available yet.')
        elif action == 'dl' and job['state'] == 'ready':
            self.store.update(job_id, state='downloading')
            self.spawn(self.perform_download(context, job_id))
            await q.edit_message_reply_markup(reply_markup=None)
        elif action == 'upload' and job['state'] == 'review':
            target = self.store.setting('target')
            if len(parts) != 3 or parts[2] != job['approval_token']:
                await q.message.reply_text('This preview is out of date. Use the latest preview.')
                return
            if not target or target != job['target']:
                await q.message.reply_text('The destination has changed. Review the updated preview.')
                await self.preview(context, job_id)
                return
            self.store.update(job_id, state='uploading', approval_token='')
            self.spawn(self.perform_upload(context, job_id))
            await q.edit_message_reply_markup(reply_markup=None)
        else:
            await q.message.reply_text('This action is no longer available.')

    async def perform_download(self, context, job_id):
        job = self.store.get(job_id)
        message = await context.bot.send_message(self.admin_id, f'Downloading #{job_id}…')
        folder = self.media_dir / str(job_id)
        folder.mkdir(exist_ok=True)
        path = folder / job["filename"]
        async def progress(done, total, speed):
            eta = max(0, (total-done)/speed) if speed else 0
            try:
                await message.edit_text(f'Downloading #{job_id}: {done/total:.0%}\n{speed/1_000_000:.2f} MB/s · ETA {eta/60:.1f} min')
            except Exception: pass
        try:
            await download(job['source'], path, job['size'], progress)
            self.store.update(job_id, state='review', path=str(path))
            await self.preview(context, job_id)
        except Exception as exc:
            log.error('Download %s failed: %s', job_id, type(exc).__name__)
            self.store.update(job_id, state='failed', error=type(exc).__name__)
            await context.bot.send_message(self.admin_id, f'Download #{job_id} failed ({type(exc).__name__}).')

    async def preview(self, context, job_id):
        job = self.store.get(job_id)
        target = self.store.setting('target', '')
        token = secrets.token_hex(4)
        self.store.update(job_id, target=target, approval_token=token)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Approve upload', callback_data=f'upload:{job_id}:{token}')]])
        await context.bot.send_message(self.admin_id, f'Ready for review #{job_id}\nName: {job["filename"]}\nSize: {job["size"]/1_000_000:.1f} MB\nSaved: {job["path"]}\nDestination: {target or "not selected"}\nUse /rename {job_id} new name before approval.', reply_markup=keyboard)

    async def perform_upload(self, context, job_id):
        job = self.store.get(job_id)
        message = await context.bot.send_message(self.admin_id, f'Uploading #{job_id}…')
        started = time.monotonic()
        last = 0.0
        async def progress(done, total):
            nonlocal last
            now = time.monotonic()
            if now-last < 3: return
            last = now
            speed = done / max(now-started, .001)
            eta = (total-done)/speed if speed else 0
            try:
                await message.edit_text(f'Uploading #{job_id}: {done/total:.0%}\n{speed/1_000_000:.2f} MB/s · ETA {eta/60:.1f} min')
            except Exception: pass
        try:
            result = await self.uploader.send_file(int(job['target']), job['path'], caption=job['title'], progress_callback=progress)
            self.store.update(job_id, state='published', telegram_message_id=result.id)
            await context.bot.send_message(self.admin_id, f'Published #{job_id}. Message ID: {result.id}')
        except Exception as exc:
            # A network error can occur after Telegram accepted the upload; never retry automatically.
            log.error('Upload %s failed or has unknown outcome: %s', job_id, type(exc).__name__)
            self.store.update(job_id, state='upload_unknown', error=type(exc).__name__)
            await context.bot.send_message(self.admin_id, f'Upload #{job_id} could not be confirmed. Check the destination before retrying.')

    async def rename(self, update, context):
        if not self.authorized(update): return
        try:
            job_id = int(context.args[0]); title = ' '.join(context.args[1:]).strip()
            job = self.store.get(job_id)
            if not job or job['state'] != 'review' or not title: raise ValueError()
            name = safe_name(title) + Path(job['filename']).suffix
            old = Path(job['path']); new = old.with_name(name)
            if new != old and new.exists(): raise ValueError()
            old.rename(new)
            self.store.update(job_id, title=title, filename=name, path=str(new))
            await self.preview(context, job_id)
        except (ValueError, IndexError, OSError):
            await update.message.reply_text('Use /rename <item number> <new name> for an item awaiting review.')

    async def pending(self, update, context):
        if not self.authorized(update): return
        rows = self.store.db.execute("SELECT id FROM jobs WHERE state='review' ORDER BY id LIMIT 20").fetchall()
        if not rows:
            await update.message.reply_text('No files awaiting review.'); return
        for row in rows:
            await self.preview(context, row['id'])

    async def saved(self, update, context):
        if not self.authorized(update): return
        rows = self.store.db.execute("SELECT * FROM jobs WHERE state IN ('oversize','reported') ORDER BY id DESC LIMIT 20").fetchall()
        if not rows:
            await update.message.reply_text('No saved large-file links.'); return
        for row in rows:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Report dead link', callback_data=f'report:{row["id"]}')]])
            await update.message.reply_text(f'#{row["id"]} {row["filename"]} · {row["size"]/1_000_000:.1f} MB\n{row["source"]}', reply_markup=keyboard, disable_web_page_preview=True)

    async def find(self, update, context):
        if not self.authorized(update): return
        query = ' '.join(context.args).strip()
        if not query:
            await update.message.reply_text('Use /find <title>.'); return
        rows = self.store.search(query)
        text = '\n'.join(f'#{r["id"]} {r["title"]} · {r["size"]/1_000_000:.1f} MB · message {r["telegram_message_id"]}' for r in rows)
        await update.message.reply_text(text or 'No published matches found.')


def main():
    load_dotenv()
    token = os.environ['BOT_TOKEN']; admin = int(os.environ['ADMIN_USER_ID'])
    api_id = int(os.environ['TELEGRAM_API_ID']); api_hash = os.environ['TELEGRAM_API_HASH']
    root = Path(os.getenv('DATA_DIR', './data')).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    media = root / 'media'; media.mkdir(exist_ok=True)
    logging.basicConfig(filename=root/'satoubot.log', level=logging.WARNING, format='%(asctime)s %(levelname)s %(message)s')
    store = Store(root/'satoubot.sqlite3')
    # An interrupted upload may already be present; require manual inspection.
    store.db.execute("UPDATE jobs SET state='upload_unknown' WHERE state='uploading'")
    store.db.execute("UPDATE jobs SET state='failed' WHERE state='downloading'")
    store.db.commit()
    uploader = TelegramClient(str(root/'user'), api_id, api_hash)
    bot = Bot(store, media, admin, uploader)
    async def post_init(app):
        await uploader.start()
        await uploader.get_dialogs()
    async def post_shutdown(app):
        for task in list(bot.tasks):
            task.cancel()
        if bot.tasks:
            await asyncio.gather(*bot.tasks, return_exceptions=True)
        await uploader.disconnect()
    endpoint = os.getenv('BOT_API_URL', 'http://127.0.0.1:8081').rstrip('/')
    application = (Application.builder().token(token).base_url(endpoint+'/bot').base_file_url(endpoint+'/file/bot')
                   .post_init(post_init).post_shutdown(post_shutdown).build())
    application.add_handler(CommandHandler('start', bot.start))
    application.add_handler(CommandHandler('status', bot.status))
    application.add_handler(CommandHandler('rename', bot.rename))
    application.add_handler(CommandHandler('find', bot.find))
    application.add_handler(CommandHandler('saved', bot.saved))
    application.add_handler(CommandHandler('pending', bot.pending))
    application.add_handler(CallbackQueryHandler(bot.callback))
    application.add_handler(MessageHandler(filters.StatusUpdate.CHAT_SHARED, bot.chat_shared))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.text))
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__': main()
