import asyncio
import time
import logging
import json
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .transfer import MAX_BYTES, download, filename_from_url, inspect_url, safe_name

log = logging.getLogger("satoubot")

class DownloadFunctions:
    async def handle_source(self, update, text):
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
            await update.message.reply_text(f'Saved large file #{job}: {name} ({size / 1_000_000:.1f} MB). Download it manually.\n{text}', reply_markup=buttons)
            return
        if content_type.lower().startswith('text/html'):
            await update.message.reply_text('This appears to be a web page, not a direct file. Page link extraction is not yet supported.')
            return
        name = filename_from_url(text)
        job = self.store.create(text, Path(name).stem, name, size)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Yes', callback_data=f'dl:{job}'), InlineKeyboardButton('No', callback_data=f'skip:{job}')]])
        await update.message.reply_text(f'Item #{job}\nName: {name}\nEstimated size: {size / 1_000_000:.1f} MB\nDownload this file?', reply_markup=keyboard)


    async def perform_download(self, context, job_id):
        job = self.store.get(job_id)
        message = None
        done_bytes=0
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
                        await message.edit_text(f'Downloading #{job_id}\n{original}\n{done_bytes/1_000_000:.1f} / {job["size"]/1_000_000:.1f} MB ({done_bytes/job["size"]:.0%})\nSpeed: {speed/1_000_000:.2f} MB/s · ETA: {eta}')
                except Exception: pass
        timer=asyncio.create_task(ticker())
        try:
            folder = self.media_dir / str(job_id)
            folder.mkdir(exist_ok=True)
            path = folder / safe_name(original)
            try:
                message = await context.bot.send_message(self.admin_id, f'Downloading #{job_id}\n{original}\nSize: {job["size"]/1_000_000:.1f} MB')
            except Exception as exc:
                log.warning('Download progress message %s failed: %s', job_id, type(exc).__name__)
            await download(job['source'], path, job['size'], progress, headers=json.loads(self.store.setting(f'job:{job_id}:headers', '{}')))
            approved=folder/job['filename']
            if approved != path: path.rename(approved)
            self.store.update(job_id, state='review', path=str(approved))
        except Exception as exc:
            log.error('Download %s failed: %s', job_id, type(exc).__name__)
            self.store.update(job_id, state='failed', error=type(exc).__name__)
            await context.bot.send_message(self.admin_id, f'Download #{job_id} failed ({type(exc).__name__}).')
            return
        finally:
            timer.cancel()
            await asyncio.gather(timer,return_exceptions=True)
        try:
            await context.bot.send_message(self.admin_id,f'Download done: {original} ({job["size"]/1_000_000:.1f} MB). Review the proposed upload name below.')
            await self.preview(context, job_id)
        except Exception as exc:
            log.warning('Preview %s failed: %s', job_id, type(exc).__name__)
