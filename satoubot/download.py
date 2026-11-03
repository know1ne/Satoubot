import asyncio
import time
import logging
import json
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .menu import navigation
from .progress import format_progress
from .file_info import probe
from .transfer import MAX_BYTES, download, filename_from_url, inspect_url, safe_name

log = logging.getLogger("satoubot")

class DownloadFunctions:
    async def handle_source(self, update, text):
        old = self.store.find_source(text)
        if old:
            await update.message.reply_text(f'This link already has item #{old["id"]} ({old["state"]}).')
            return
        info=await probe(text)
        if info.get('status') != 'ready' or not info.get('size'):
            reason=info.get('reason','Size unknown')
            retry=info.get('retry_after')
            manual = self.store.cache('manual_link', {'url':text})
            keyboard=InlineKeyboardMarkup([[InlineKeyboardButton('Give link', callback_data=f'directmanual:{manual}')], navigation()])
            if info.get('status') == 'temporary':
                key=self.store.cache('direct_retry', {'url':text})
                keyboard=InlineKeyboardMarkup([[InlineKeyboardButton('Retry check', callback_data=f'directretry:{key}'), InlineKeyboardButton('Give link', callback_data=f'directmanual:{manual}')], navigation()])
            await update.message.reply_text(f"{reason}." + (f" Retry after {retry}." if retry else ''), reply_markup=keyboard)
            return
        size, content_type=info['size'],info.get('content_type','')
        if size > MAX_BYTES:
            name = info.get('filename') or filename_from_url(text)
            job = self.store.create(text, Path(name).stem, name, size)
            self.store.update(job, state='oversize')
            buttons = InlineKeyboardMarkup([[InlineKeyboardButton('Report dead link', callback_data=f'report:{job}')]])
            await update.message.reply_text(f'Saved large file #{job}: {name} ({size / 1_000_000:.1f} MB). Download it manually.\n{text}', reply_markup=buttons)
            return
        if content_type.lower().startswith('text/html'):
            await update.message.reply_text('This appears to be a web page, not a direct file. Page link extraction is not yet supported.')
            return
        name = info.get('filename') or filename_from_url(text)
        job = self.store.create(text, Path(name).stem, name, size)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Transfer', callback_data=f'dl:{job}'), InlineKeyboardButton('Give link', callback_data=f'directlink:{job}'), InlineKeyboardButton('No', callback_data=f'skip:{job}')], navigation()])
        await update.message.reply_text(f'Item #{job}\nName: {name}\nEstimated size: {size / 1_000_000:.1f} MB\nDownload this file?', reply_markup=keyboard)


    async def perform_download(self, context, job_id):
        job = self.store.get(job_id)
        message = None
        done_bytes=0
        stage='Checking'
        last_bytes=0
        started=time.monotonic()
        original=self.store.setting(f'job:{job_id}:original',job['filename'])
        async def progress(done,total,speed):
            nonlocal done_bytes
            done_bytes=done
        async def ticker():
            nonlocal last_bytes
            last_time=started
            while True:
                await asyncio.sleep(5)
                now=time.monotonic()
                speed=(done_bytes-last_bytes)/max(now-last_time,.001)
                last_bytes=done_bytes;last_time=now
                eta=f'{(job["size"]-done_bytes)/speed/60:.1f} min' if speed else 'waiting for data'
                try:
                    if message:
                        await message.edit_text(format_progress(f'{stage} download #{job_id}',original,done_bytes,job['size'],speed))
                    await self.panel_status(context, 'transfer', format_progress(f'{stage} download #{job_id}', original, done_bytes, job['size'], speed))
                except Exception: pass
        timer=asyncio.create_task(ticker())
        try:
            await self.panel_status(context, 'transfer', f'Downloading #{job_id} · {original}')
            folder = self.media_dir / str(job_id)
            folder.mkdir(exist_ok=True)
            path = folder / safe_name(original)
            try:
                message = await context.bot.send_message(self.admin_id, f'Connecting download #{job_id}\n{original}\nSize: {job["size"]/1_000_000:.1f} MB')
            except Exception as exc:
                log.warning('Download progress message %s failed: %s', job_id, type(exc).__name__)
            if message:
                try: await message.edit_text(f'Checking source for {original}…')
                except Exception: pass
            source_headers=json.loads(self.store.setting(f'job:{job_id}:headers', '{}'))
            check=await probe(job['source'],source_headers)
            if check.get('status') != 'ready' or check.get('size') != job['size']:
                self.store.update(job_id, state='failed', error='Source changed')
                await context.bot.send_message(self.admin_id, f"Link status changed: {check.get('reason','size changed')}. Search again before retrying.")
                return
            if not self.transfers_enabled():
                self.store.update(job_id, state='cancelled', error='Transfers switched off')
                if message: await message.edit_text('Transfers are off. Download cancelled before starting.')
                return
            stage='Transferring'
            await download(job['source'], path, job['size'], progress, headers=source_headers)
            stage='Checking'
            approved=folder/job['filename']
            if approved != path: path.rename(approved)
            self.store.update(job_id, state='review', path=str(approved))
        except asyncio.CancelledError:
            self.store.update(job_id, state='cancelled', error='Cancelled')
            if message:
                try: await message.edit_text(f'Download cancelled: {original}')
                except Exception: pass
            raise
        except Exception as exc:
            log.error('Download %s failed: %s', job_id, type(exc).__name__)
            self.store.update(job_id, state='failed', error=type(exc).__name__)
            if message:
                try: await message.edit_text(f'Download failed: {original}. Search again to retry.')
                except Exception: pass
            await context.bot.send_message(self.admin_id, f'Download #{job_id} failed ({type(exc).__name__}). Check the source and search again to retry.')
            return
        finally:
            timer.cancel()
            await asyncio.gather(timer,return_exceptions=True)
            await self.panel_status(context, 'transfer')
        try:
            await context.bot.send_message(self.admin_id,f'Download done: {original} ({job["size"]/1_000_000:.1f} MB). Review the proposed upload name below.')
            await self.preview(context, job_id)
        except Exception as exc:
            log.warning('Preview %s failed: %s', job_id, type(exc).__name__)
