"""Bounded, opt-in MTProto upload of big-file parts on the user session."""
import asyncio
from pathlib import Path

from telethon import functions, helpers, types

PART_SIZE = 512 * 1024


async def upload_parts(client, path, expected_size, workers, progress):
    """Upload big-file parts without sending a message; return a Telegram handle."""
    path = Path(path)
    if not 2 <= workers <= 4:
        raise ValueError('Parallel upload requires 2 to 4 workers')
    if expected_size <= 10 * 1024 * 1024 or path.stat().st_size != expected_size:
        raise ValueError('File size changed or is too small for parallel upload')
    count = (expected_size + PART_SIZE - 1) // PART_SIZE
    file_id = helpers.generate_random_long()
    queue = asyncio.Queue(maxsize=workers * 2)
    completed = 0

    async def worker():
        nonlocal completed
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                index, chunk = item
                saved = await client(functions.upload.SaveBigFilePartRequest(file_id, index, count, chunk))
                if not saved:
                    raise RuntimeError(f'Telegram did not accept part {index}')
                completed += len(chunk)
                await progress(completed, expected_size)
            finally:
                queue.task_done()

    with path.open('rb') as source:
        async with asyncio.TaskGroup() as group:
            for _ in range(workers): group.create_task(worker())
            for index in range(count):
                chunk = source.read(PART_SIZE)
                expected = min(PART_SIZE, expected_size - index * PART_SIZE)
                if len(chunk) != expected:
                    raise ValueError('File size changed during upload')
                await queue.put((index, chunk))
            if source.read(1):
                raise ValueError('File size changed during upload')
            for _ in range(workers): await queue.put(None)
    return types.InputFileBig(file_id, count, path.name)
