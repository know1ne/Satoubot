import asyncio
import logging
import os
import secrets
import tempfile
import warnings
from pathlib import Path

import psutil
from PIL import Image, ImageOps
from dotenv import load_dotenv
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                      KeyboardButtonRequestChat, ReplyKeyboardMarkup, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          MessageHandler, InlineQueryHandler, filters)

from .store import Store
from .transfer import safe_name
from .download import DownloadFunctions
from .upload import UploadFunctions
from .search import SearchFunctions
from .group_management import GroupManagementFunctions
from .migration import MigrationFunctions
from .sessions import SessionManager
from .destinations import DestinationFunctions
from .catalogue import CatalogueFunctions
from .discovery import Discovery

log = logging.getLogger('satoubot')


class Bot(DestinationFunctions, CatalogueFunctions, DownloadFunctions, UploadFunctions, SearchFunctions, GroupManagementFunctions, MigrationFunctions):
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

    def transfers_enabled(self):
        return self.store.setting('transfers_enabled', '1') == '1'

    def transfer_busy(self):
        return any(not task.done() for task in self.tasks)

    async def transfer_gate(self, message):
        if not self.transfers_enabled():
            await message.reply_text('Transfers are off. Press Start transfers to enable downloads and uploads.')
            return False
        if self.transfer_busy():
            await message.reply_text('A transfer is processing. Please wait until it finishes, then try again.')
            return False
        return True

    async def start(self, update, context):
        if not self.authorized(update): return
        if context.args and context.args[0].startswith('movie_'):
            try:
                key = int(context.args[0].split('_', 1)[1])
            except ValueError:
                return
            if getattr(self, 'discovery_tasks', None):
                await update.message.reply_text('A search is processing. Please wait.')
                return
            self.launch_discovery(self.safe_movie_card(context, key))
            return
        keyboard = [[KeyboardButton('Start transfers'), KeyboardButton('Stop transfers')],
                    [KeyboardButton('Download'), KeyboardButton('Uploads awaiting approval')],
                    [KeyboardButton('Search'), KeyboardButton('Saved links')],
                    [KeyboardButton('Add-ons'), KeyboardButton('Library search')],
                    [KeyboardButton('My channels')],
                    [KeyboardButton('Group management'), KeyboardButton('Migration')],
                    [KeyboardButton('Select channel', request_chat=KeyboardButtonRequestChat(request_id=1, chat_is_channel=True, request_title=True))],
                    [KeyboardButton('Select group', request_chat=KeyboardButtonRequestChat(request_id=2, chat_is_channel=False, request_title=True))],
                    [KeyboardButton('Status'), KeyboardButton('Help')]]
        state = 'on' if self.transfers_enabled() else 'off'
        await update.message.reply_text(f'Transfers are {state}. Choose a destination, then send a direct media URL. Every upload requires approval. Use Add-ons to add your catalogue manifest, then Search for a movie.', reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

    async def status(self, update, context):
        if not self.authorized(update): return
        process = psutil.Process()
        memory = process.memory_info().rss / (1024 * 1024)
        cpu = psutil.cpu_percent(interval=None)
        target = self.store.setting('target', 'not selected')
        state = 'on' if self.transfers_enabled() else 'off'
        await update.message.reply_text(f'Transfers: {state}\nDestination: {target}\nSystem CPU: {cpu:.1f}%\nBot memory: {memory:.1f} MiB\nActive transfers: {sum(not task.done() for task in self.tasks)}')

    async def chat_shared(self, update, context):
        if not self.authorized(update): return
        chat = update.message.chat_shared
        self.store.set_setting('target', chat.chat_id)
        await update.message.reply_text(f'Destination selected: {chat.title or chat.chat_id}. Ensure the uploading account can post there and the bot has the required admin access.')

    async def text(self, update, context):
        if not self.authorized(update): return
        text = update.message.text.strip()
        if text == 'My channels': return await self.destination_menu(context)
        if text == 'Status': return await self.status(update, context)
        if text == 'Help': return await self.start(update, context)
        if text in ('Start transfers', 'Stop transfers'):
            enabled = text == 'Start transfers'
            self.store.set_setting('transfers_enabled', '1' if enabled else '0')
            notice = ' Transfers already running will finish.' if not enabled and self.transfer_busy() else ''
            await update.message.reply_text(f'Transfers are {"on" if enabled else "off"}.{notice}')
            return
        if text in ('Download', 'Search', 'Library search', 'Add-ons'):
            for key in ('manifest_pending', 'catalogue_search_pending', 'search_pending'):
                context.user_data.pop(key, None)
        if text == 'Download':
            await update.message.reply_text('Send a direct HTTP(S) media link. The bot will check the size before asking to download.')
            return
        if text == 'Uploads awaiting approval': return await self.pending(update, context)
        if text == 'Add-ons': return await self.addons(update, context)
        if text == 'Search':
            context.user_data['catalogue_search_pending'] = True
            await update.message.reply_text('Send a movie name to search catalogues. Searching will not download anything.')
            return
        if text == 'Library search':
            context.user_data['search_pending'] = True
            await update.message.reply_text('Send a title to search published items, or use /find <title>.')
            return
        if text == 'Saved links': return await self.saved(update, context)
        if text == 'Group management': return await self.group_menu(update, context)
        if text == 'Migration': return await self.migration_menu(update, context)
        if context.user_data.pop('manifest_pending', False):
            try:
                uploaded = context.user_data.pop('uploaded_manifest', None)
                if uploaded is not None:
                    name, kind = Discovery(self.store).register(text, uploaded)
                else:
                    name, kind = await Discovery(self.store).add(text)
                await update.message.reply_text(f'Saved {name} ({kind}). Use /search <movie name>.')
            except Exception:
                await update.message.reply_text('Could not read that manifest. Open Add-ons and send its original manifest URL.')
            return
        if context.user_data.pop('catalogue_search_pending', False):
            return await self.begin_search(update.message, text)
        if context.user_data.pop('search_pending', False):
            return await self.search_query(update, text)
        if not text.startswith(('https://', 'http://')):
            return await self.begin_search(update.message, text)
        await self.handle_source(update, text)

    async def callback(self, update, context):
        if not self.authorized(update): return
        q = update.callback_query
        await q.answer()
        if q.data == 'destcancel' or q.data.startswith(('dest:', 'choose_dest:')):
            return await self.select_destination(update, context)
        if q.data in ('defaultproviders', 'searchagain') or q.data.startswith(('movie:', 'file:', 'page:', 'cancelpage:', 'yes:', 'no:')):
            return await self.catalogue_callback(update, context)
        try:
            parts = q.data.split(':')
            action, job_id = parts[0], int(parts[1])
            job = self.store.get(job_id)
        except (ValueError, AttributeError): return
        if not job: return
        if action == 'report' and job['state'] in ('oversize', 'reported', 'link_only'):
            self.store.update(job_id, state='reported')
            await q.message.reply_text('Link reported and saved. Automatic source searches are not available yet.')
        elif action == 'thumb' and job['state'] == 'review':
            if len(parts) != 3 or parts[2] != job['approval_token']:
                await q.message.reply_text('This preview is out of date. Use the latest preview.')
                return
            context.user_data['thumbnail_request'] = (job_id, parts[2])
            await q.message.reply_text(f'Send a photo for item #{job_id}. Use /pending to return to the preview.')
        elif action == 'skip' and job['state'] == 'ready':
            self.store.update(job_id,state='cancelled')
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text('Download cancelled.')
        elif action == 'dl' and job['state'] == 'ready':
            if not await self.transfer_gate(q.message): return
            self.store.update(job_id, state='downloading')
            self.spawn(self.perform_download(context, job_id))
            await q.edit_message_reply_markup(reply_markup=None)
        elif action == 'upload' and job['state'] == 'review':
            target = self.store.setting('target')
            if len(parts) != 3 or parts[2] != job['approval_token']:
                await q.message.reply_text('This preview is out of date. Use the latest preview.')
                return
            if not await self.transfer_gate(q.message): return
            if not target:
                await self.destination_menu(context,job_id)
                return
            if target != job['target']:
                await q.message.reply_text('The destination has changed. Review the updated preview.')
                await self.preview(context, job_id)
                return
            try:
                member=await context.bot.get_chat_member(int(target),context.bot.id)
                if member.status!='administrator' or member.can_post_messages is False:
                    raise ValueError()
            except Exception:
                await q.message.reply_text('The bot needs administrator access and permission to post in this destination. Select a channel again.')
                await self.destination_menu(context,job_id)
                return
            if not await self.transfer_gate(q.message): return
            self.store.update(job_id, state='uploading', approval_token='')
            self.spawn(self.perform_upload(context, job_id))
            await q.edit_message_reply_markup(reply_markup=None)
        else:
            await q.message.reply_text('This action is no longer available.')


    async def preview(self, context, job_id):
        job = self.store.get(job_id)
        target = self.store.setting('target', '')
        token = secrets.token_hex(4)
        self.store.update(job_id, target=target, approval_token=token)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Approve upload', callback_data=f'upload:{job_id}:{token}')],
                                         [InlineKeyboardButton('Set thumbnail', callback_data=f'thumb:{job_id}:{token}')],
                                         [InlineKeyboardButton('Select channel', callback_data=f'choose_dest:{job_id}')]])
        text = f'Ready for review #{job_id}\nName: {job["filename"]}\nSize: {job["size"]/1_000_000:.1f} MB\nSaved: {job["path"]}\nDestination: {target or "not selected"}\nUse /rename {job_id} new name before approval.'
        if job['thumbnail']:
            with open(job['thumbnail'], 'rb') as photo:
                await context.bot.send_photo(self.admin_id, photo=photo, caption=text, reply_markup=keyboard)
        else:
            await context.bot.send_message(self.admin_id, text, reply_markup=keyboard)
        if not target: await self.destination_menu(context,job_id)

    async def thumbnail_photo(self, update, context):
        if not self.authorized(update): return
        request = context.user_data.pop('thumbnail_request', None)
        if not request:
            await update.message.reply_text('Choose Set thumbnail on an item preview first.')
            return
        job_id, token = request
        job = self.store.get(job_id)
        if not job or job['state'] != 'review' or job['approval_token'] != token:
            await update.message.reply_text('This preview is out of date. Use /pending to select the latest one.')
            return
        photo = update.message.photo[-1]
        if photo.file_size and photo.file_size > 10_000_000:
            await update.message.reply_text('Photo is too large. Choose Set thumbnail and send a photo under 10 MB.')
            return
        folder = self.media_dir / str(job_id)
        folder.mkdir(exist_ok=True)
        source = None
        output = folder / f'thumb-{secrets.token_hex(8)}.jpg'
        try:
            with tempfile.NamedTemporaryFile(dir=folder, delete=False) as temporary:
                source = Path(temporary.name)
            remote = await photo.get_file()
            await remote.download_to_drive(custom_path=source)
            if source.stat().st_size > 10_000_000:
                raise ValueError('photo too large')
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(source) as image:
                    image = ImageOps.exif_transpose(image).convert('RGB')
                    image.thumbnail((320, 320))
                    for quality in (85, 70, 55, 40, 25):
                        image.save(output, 'JPEG', quality=quality, optimize=True)
                        if output.stat().st_size < 20_000:
                            break
            if output.stat().st_size >= 20_000:
                raise ValueError('thumbnail too large')
            job = self.store.get(job_id)
            if not job or job['state'] != 'review' or job['approval_token'] != token:
                output.unlink(missing_ok=True)
                await update.message.reply_text('This preview is out of date. Use /pending to select the latest one.')
                return
            previous = job['thumbnail']
            self.store.update(job_id, thumbnail=str(output), approval_token='')
            if previous:
                try:
                    Path(previous).unlink(missing_ok=True)
                except OSError:
                    log.warning('Could not remove old thumbnail for item %s', job_id)
        except Exception as exc:
            output.unlink(missing_ok=True)
            log.warning('Thumbnail %s failed: %s', job_id, type(exc).__name__)
            await update.message.reply_text('Could not use that photo. Choose Set thumbnail and try another photo.')
            return
        finally:
            if source: source.unlink(missing_ok=True)
        await self.preview(context, job_id)


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
    sessions = SessionManager(root, api_id, api_hash)
    uploader = sessions.user
    bot = Bot(store, media, admin, uploader)

    async def post_init(app):
        await sessions.start()
    async def post_shutdown(app):
        for task in list(bot.tasks):
            task.cancel()
        if bot.tasks:
            await asyncio.gather(*bot.tasks, return_exceptions=True)
        discovery_tasks = list(getattr(bot, 'discovery_tasks', set()))
        for task in discovery_tasks: task.cancel()
        if discovery_tasks: await asyncio.gather(*discovery_tasks, return_exceptions=True)
        await sessions.stop()
    endpoint = os.getenv('BOT_API_URL', 'http://127.0.0.1:8081').rstrip('/')
    application = (Application.builder().token(token).base_url(endpoint+'/bot').base_file_url(endpoint+'/file/bot')
                   .post_init(post_init).post_shutdown(post_shutdown).build())
    application.add_handler(CommandHandler('start', bot.start))
    application.add_handler(CommandHandler('status', bot.status))
    application.add_handler(CommandHandler('rename', bot.rename))
    application.add_handler(CommandHandler('find', bot.find))
    application.add_handler(CommandHandler('search', bot.online_search))
    application.add_handler(CommandHandler('addons', bot.addons))
    application.add_handler(InlineQueryHandler(bot.inline_search))
    application.add_handler(CommandHandler('saved', bot.saved))
    application.add_handler(CommandHandler('pending', bot.pending))
    application.add_handler(CallbackQueryHandler(bot.callback))
    application.add_handler(MessageHandler(filters.StatusUpdate.CHAT_SHARED, bot.chat_shared))
    application.add_handler(MessageHandler(filters.PHOTO, bot.thumbnail_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, bot.manifest_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.text))
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__': main()
