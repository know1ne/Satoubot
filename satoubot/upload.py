import logging
import time

log = logging.getLogger("satoubot")

class UploadFunctions:
    async def perform_upload(self, context, job_id):
        job = self.store.get(job_id)
        try:
            message = await context.bot.send_message(self.admin_id, f'Uploading #{job_id}…')
        except Exception as exc:
            message = None
            log.warning('Upload progress message %s failed: %s', job_id, type(exc).__name__)
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
                if message:
                    await message.edit_text(f'Uploading #{job_id}: {done/total:.0%}\n{speed/1_000_000:.2f} MB/s · ETA {eta/60:.1f} min')
            except Exception: pass
        try:
            result = await self.uploader.send_file(int(job['target']), job['path'], caption=job['title'], thumb=job['thumbnail'] or None, progress_callback=progress)
        except Exception as exc:
            # A network error can occur after Telegram accepted the upload; never retry automatically.
            log.error('Upload %s failed or has unknown outcome: %s', job_id, type(exc).__name__)
            self.store.update(job_id, state='upload_unknown', error=type(exc).__name__)
            await context.bot.send_message(self.admin_id, f'Upload #{job_id} could not be confirmed. Check the destination before retrying.')
            return
        self.store.update(job_id, state='published', telegram_message_id=result.id)
        try:
            await context.bot.send_message(self.admin_id, f'Published #{job_id}. Message ID: {result.id}')
        except Exception as exc:
            log.warning('Publication notification %s failed: %s', job_id, type(exc).__name__)
