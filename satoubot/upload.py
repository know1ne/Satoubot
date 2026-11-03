import asyncio
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telethon import utils
from .parallel_upload import upload_parts
from .progress import format_progress

log = logging.getLogger('satoubot')


class UploadFunctions:
    async def perform_upload(self, context, job_id):
        job = self.store.get(job_id)
        try:
            await self.panel_status(context, 'transfer', f'Uploading #{job_id} · {job["filename"]}')
            message = await context.bot.send_message(self.admin_id, f'Connecting upload #{job_id}\n{job["filename"]}')
        except asyncio.CancelledError:
            self.store.update(job_id, state='upload_unknown', error='Cancelled')
            await self.panel_status(context, 'transfer')
            raise
        except Exception as exc:
            message = None
            log.warning('Upload progress message %s failed: %s', job_id, type(exc).__name__)
        done_bytes, total_bytes = 0, job['size']
        async def progress(done, total):
            nonlocal done_bytes, total_bytes
            done_bytes, total_bytes = done, total
        async def ticker():
            previous, last = 0, time.monotonic()
            while True:
                await asyncio.sleep(5)
                now = time.monotonic()
                speed = (done_bytes-previous)/max(now-last,.001)
                previous, last = done_bytes, now
                try:
                    if message:
                        await message.edit_text(format_progress(f'Transferring upload #{job_id}',job['filename'],done_bytes,total_bytes,speed))
                    await self.panel_status(context, 'transfer', format_progress(f'Transferring upload #{job_id}', job['filename'], done_bytes, total_bytes, speed))
                except Exception:
                    pass
        timer = asyncio.create_task(ticker())
        try:
            upload_file = job['path']
            extra = {}
            if self.upload_parallel_parts > 1 and job['size'] > 10 * 1024 * 1024:
                # Keep the same filename, MIME type and video attributes as send_file(path).
                extra['attributes'], extra['mime_type'] = utils.get_attributes(job['path'], thumb=job['thumbnail'] or None)
                upload_file = await upload_parts(self.uploader, job['path'], job['size'], self.upload_parallel_parts, progress)
            result = await self.uploader.send_file(int(job['target']), upload_file, caption=job['title'], thumb=job['thumbnail'] or None, progress_callback=progress, **extra)
            self.store.update(job_id, state='published', telegram_message_id=result.id)
            self.daily_init()
            self.store.db.execute("UPDATE daily_titles SET state='published', published_day=? WHERE job_id=?", (self.daily_day(), job_id))
            self.store.db.commit()
        except asyncio.CancelledError:
            self.store.update(job_id, state='upload_unknown', error='Cancelled')
            if message:
                try: await message.edit_text('Upload cancelled. Check the destination before retrying: Telegram may already have accepted it.')
                except Exception: pass
            raise
        except Exception as exc:
            # Telegram may already have accepted the upload; never retry automatically.
            log.error('Upload %s failed or has unknown outcome: %s', job_id, type(exc).__name__)
            self.store.update(job_id, state='upload_unknown', error=type(exc).__name__)
            if message:
                try: await message.edit_text('Upload failed or outcome unknown. Check the destination before retrying.')
                except Exception: pass
            await context.bot.send_message(self.admin_id, f'Upload #{job_id} could not be confirmed. Check the destination before retrying.')
            return
        finally:
            timer.cancel()
            await asyncio.gather(timer,return_exceptions=True)
            await self.panel_status(context, 'transfer')
        if message:
            try: await message.edit_text(f'Upload completed: {job["filename"]}')
            except Exception: pass
        try:
            target = str(job['target'])
            link = f'https://t.me/c/{target[4:]}/{result.id}' if target.startswith('-100') else None
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Open destination', url=link)]]) if link else None
            await context.bot.send_message(self.admin_id, f'Completed #{job_id}\n{job["filename"]}\n{job["size"]/1_000_000:.1f} MB\nDestination: {target}\nMessage ID: {result.id}', reply_markup=keyboard)
        except Exception as exc:
            log.warning('Publication notification %s failed: %s', job_id, type(exc).__name__)
