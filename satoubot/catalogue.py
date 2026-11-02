import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlparse

from telegram import (InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup,
                      InlineQueryResultArticle, InputTextMessageContent)
from .menu import navigation
from .artwork import landscape
from .discovery import Discovery, DEFAULT_PROVIDERS
from .transfer import MAX_BYTES, inspect_url, safe_name
from .file_info import probe, clean_title


class CatalogueFunctions:
    async def addons(self, update, context):
        if not self.authorized(update): return
        context.user_data.pop('uploaded_manifest', None)
        context.user_data['manifest_pending'] = True
        names = [m.get('name', 'Add-on') for _, _, m in Discovery(self.store).manifests()]
        await update.message.reply_text('Send the catalogue/add-on manifest URL (Stremio format), or a Nuvio provider manifest URL.\nSaved: ' + (', '.join(names) or 'none') + '\nUse /search <movie name> after adding a catalogue.',
            reply_markup=Markup([[Button('Add Yoru providers', callback_data='defaultproviders')], navigation()]))

    async def manifest_document(self, update, context):
        if not self.authorized(update): return
        document = update.message.document
        if not context.user_data.get('manifest_pending'):
            await update.message.reply_text('Open Add-ons first to import a manifest file.')
            return
        if not document.file_size or document.file_size > 1_000_000:
            await update.message.reply_text('Send a JSON manifest under 1 MB, or paste its URL.')
            return
        try:
            remote = await document.get_file()
            data = await remote.download_as_bytearray()
            if len(data) > 1_000_000: raise ValueError()
            manifest = json.loads(data)
            if not isinstance(manifest, dict): raise ValueError()
            context.user_data['uploaded_manifest'] = manifest
            await update.message.reply_text('Now send the original manifest URL or repository base URL. The bot needs it to resolve catalogue endpoints and plugin filenames.')
        except Exception:
            await update.message.reply_text('Could not read this JSON manifest. Paste the original manifest URL instead.')

    def launch_discovery(self, coro):
        if not hasattr(self, 'discovery_tasks'): self.discovery_tasks = set()
        task = asyncio.create_task(coro)
        self.discovery_tasks.add(task)
        task.add_done_callback(self.discovery_tasks.discard)

    async def online_search(self, update, context):
        if not self.authorized(update): return
        for key in ('manifest_pending', 'catalogue_search_pending', 'search_pending'):
            context.user_data.pop(key, None)
        query = ' '.join(context.args).strip()
        if not query:
            context.user_data['catalogue_search_pending'] = True
            await update.message.reply_text('Send a movie name. Searching does not start a download. For inline search, type @your_bot_username followed by the title in Telegram.')
            return
        await self.begin_search(update.message, query)

    async def begin_search(self, message, query):
        if getattr(self, 'discovery_tasks', None):
            await message.reply_text('A search is processing. Please wait until it finishes.')
            return
        if not any(k == 'addon' for _, k, _ in Discovery(self.store).manifests()):
            await message.reply_text('Please add your catalogue manifest URL using Add-ons first.')
            return
        await message.reply_text(f'Searching for {query[:120]}…')
        self.launch_discovery(self.search_results(message, query))

    async def search_results(self, message, query):
        try:
            movies, errors = await Discovery(self.store).search(query)
            buttons = []
            for movie in movies:
                key = self.store.cache('movie', movie)
                label = f'{movie["name"]} ({movie.get("year") or movie.get("releaseInfo") or "year unknown"})'
                buttons.append([Button(label[:100], callback_data=f'movie:{key}')])
            buttons.append(navigation())
            text = 'Choose a movie to see its image, details and available files. No download starts until you select one.' if movies else 'No matching movies found.'
            if errors: text += '\nSome catalogues failed. Try again later.'
            await message.reply_text(text, reply_markup=Markup(buttons) if buttons else None)
        except Exception:
            await message.reply_text('Search failed. Please try again later.')

    async def movie_card(self, context, key):
        movie = self.store.cached(key, 'movie')
        if not movie:
            await context.bot.send_message(self.admin_id, 'Search result expired. Use /search again.')
            return
        service = Discovery(self.store)
        try:
            movie = await service.details(movie)
        except Exception:
            pass
        semaphore = asyncio.Semaphore(4)
        lock = asyncio.Lock()
        source_ids, checked, errors, tasks, seen = [], [], [], set(), set()
        selection = self.store.cache('pages', [])
        if not hasattr(self, 'selection_pages'): self.selection_pages = {}
        if len(self.selection_pages) >= 512: self.selection_pages.pop(next(iter(self.selection_pages)))
        self.selection_pages[selection] = 0
        pending_render = None
        card = None
        card_is_photo = False
        last_update = 0.0
        artwork = movie.get('background') or movie.get('poster')
        artwork_task = asyncio.create_task(landscape(artwork)) if artwork else None

        async def render(final=False):
            nonlocal card, card_is_photo, last_update
            # Cancellation removes this selection; don't revive it on a later result.
            cursor = self.store.db.execute("UPDATE discovery_cache SET payload=? WHERE id=? AND kind='pages'", (json.dumps(source_ids), selection))
            self.store.db.commit()
            if not cursor.rowcount: return
            qualities = ', '.join(dict.fromkeys(str(s.get('quality') or 'unspecified')[:35] for s in checked))[:250]
            caption = f'{movie["name"][:150]}\nYear: {movie.get("year") or movie.get("releaseInfo") or "unknown"}\nQualities: {qualities or "none found"}\nAvailable files: {len(source_ids)}'
            caption += '\nSelect a file to download. Larger or unsupported files can be saved as links.'
            if not final: caption += '\nSearching other providers…'
            if errors: caption += f'\n{len(errors)} source(s) failed or need configuration; search again to retry.'
            for error in dict.fromkeys(e for e in errors if e.startswith('Plugin runtime:')):
                caption += '\n' + error
            if final and not source_ids: caption += '\nNo playable links returned. Try another source or movie.'
            markup = self.file_page(selection, source_ids, self.selection_pages.get(selection, 0))
            if card is None:
                try:
                    if not artwork_task: raise ValueError()
                    photo = await asyncio.wait_for(asyncio.shield(artwork_task), 2)
                    card = await context.bot.send_photo(self.admin_id, photo=photo, caption=caption, reply_markup=markup)
                    card_is_photo = True
                except Exception:
                    card = await context.bot.send_message(self.admin_id, caption + '\nImage unavailable.', reply_markup=markup)
            else:
                try:
                    if card_is_photo: await card.edit_caption(caption=caption, reply_markup=markup)
                    else: await card.edit_text(caption, reply_markup=markup)
                except Exception:
                    # A failed edit does not change file state or start a transfer.
                    pass
            last_update = time.monotonic()

        async def flush():
            await asyncio.sleep(max(0, 1 - (time.monotonic() - last_update)))
            async with lock:
                await render()

        async def inspect(stream):
            nonlocal pending_render
            async with semaphore:
                stream = dict(stream)
                stream['size_verified'] = False
                stream['file_type'] = Path(stream.get('filename') or urlparse(stream['url']).path).suffix.lstrip('.').lower() or 'unknown'
                headers = stream.get('headers') or {}
                stream['headers'] = {str(k): str(v) for k,v in headers.items()} if isinstance(headers, dict) else {}
                try:
                    info = await asyncio.wait_for(probe(stream['url'], stream['headers']), 8)
                    stream['check_status'] = info.get('status','ready')
                    stream['check_reason'] = info.get('reason','')
                    stream['retry_after'] = info.get('retry_after')
                    if stream['check_status'] == 'ready':
                        size, content_type = info['size'], info['content_type']
                        stream['filename'] = info['filename'] if Path(info['filename']).suffix.lower() in ('.mkv','.mp4','.avi','.webm','.mov') else stream.get('filename') or info['filename']
                        stream['file_type'] = Path(stream['filename']).suffix.lstrip('.').lower() or stream['file_type']
                        stream['file_type'] = 'hls' if 'mpegurl' in content_type.lower() else stream['file_type']
                        stream['file_type'] = 'page' if 'text/html' in content_type.lower() else stream['file_type']
                        stream['size'] = size
                        stream['size_verified'] = bool(size)
                        if stream['file_type'] in ('hls', 'm3u8', 'mpd', 'page') or 'dash+xml' in content_type.lower():
                            stream['size'] = None
                            stream['size_verified'] = False
                except Exception:
                    stream['check_status'] = 'temporary'
                    stream['check_reason'] = 'Link check failed'
            async with lock:
                if self.store.cached(selection, 'pages') is None: return
                # Keep unverified and failed links out of the persistent ready list.
                stream['movie'] = movie
                if stream.get('size_verified') and stream.get('size') and Path(stream.get('filename') or '').suffix.lower() in ('.mp4','.mkv','.avi','.mov','.webm'):
                    stream['identity'] = movie['id'] + ':' + stream['filename'].casefold() + ':' + str(stream['size'])
                kind = 'stream' if stream.get('check_status') in ('ready','manual') else 'unchecked'
                source_key = self.store.cache(kind, stream)
                if kind == 'unchecked': source_key = -source_key
                if source_key not in source_ids:
                    source_ids.append(source_key)
                    checked.append(stream)
                if card is None or time.monotonic() - last_update >= 1:
                    await render()
                elif pending_render is None or pending_render.done():
                    pending_render = asyncio.create_task(flush())

        async def on_result(found, error):
            if error and error not in errors: errors.append(error)
            for stream in found:
                if stream['url'] in seen: continue
                seen.add(stream['url'])
                task = asyncio.create_task(inspect(stream))
                tasks.add(task)

        try:
            streams, final_errors = await service.streams(movie, on_result=on_result)
            for error in final_errors:
                if error not in errors: errors.append(error)
            # Also supports providers returning all results in one batch.
            await on_result(streams, None)
            if tasks: await asyncio.gather(*tasks)
            if pending_render and not pending_render.done():
                pending_render.cancel()
                await asyncio.gather(pending_render, return_exceptions=True)
            async with lock:
                await render(final=True)
        finally:
            if pending_render and not pending_render.done(): pending_render.cancel()
            if pending_render: await asyncio.gather(pending_render, return_exceptions=True)
            for task in tasks: task.cancel()
            if tasks: await asyncio.gather(*tasks, return_exceptions=True)
            if artwork_task:
                if not artwork_task.done(): artwork_task.cancel()
                await asyncio.gather(artwork_task, return_exceptions=True)

    async def catalogue_callback(self, update, context):
        q = update.callback_query
        if q.data == 'defaultproviders':
            try:
                name, _ = await Discovery(self.store).add(DEFAULT_PROVIDERS)
                await q.message.reply_text(f'Saved {name}. Add your catalogue manifest next if you have not added one.')
                context.user_data.pop('manifest_pending', None)
            except Exception:
                await q.message.reply_text('Could not load the provider manifest. Try again later.')
            return
        if q.data == 'searchagain':
            context.user_data['catalogue_search_pending'] = True
            await q.message.reply_text('Send another movie name.')
            return
        try:
            action, raw = q.data.split(':', 1); key = int(raw)
        except (ValueError, AttributeError): return
        if action == 'page':
            selection, page = divmod(key, 1000)
            ids = self.store.cached(selection, 'pages')
            if ids is None:
                await q.message.reply_text('Results expired or cancelled. Search again.'); return
            if not hasattr(self, 'selection_pages'): self.selection_pages = {}
            self.selection_pages[selection] = page
            await q.edit_message_reply_markup(reply_markup=self.file_page(selection, ids, page))
        elif action == 'cancelpage':
            getattr(self, 'selection_pages', {}).pop(key, None)
            self.store.db.execute("DELETE FROM discovery_cache WHERE id=? AND kind='pages'",(key,));self.store.db.commit()
            await q.edit_message_reply_markup(reply_markup=None)
            await self.main_menu(q.message)
        elif action in ('yes', 'no'):
            stream = self.store.cached(key, 'confirmation')
            if not stream:
                await q.message.reply_text('This confirmation expired or was already used.'); return
            if action == 'yes' and not await self.transfer_gate(q.message): return
            self.store.db.execute("DELETE FROM discovery_cache WHERE id=? AND kind='confirmation'",(key,));self.store.db.commit()
            if action == 'yes':
                self.active_description = f'Checking {stream.get("filename") or stream["movie"]["name"]}'
                self.spawn(self.download_choice(context, stream))
            await q.edit_message_reply_markup(reply_markup=None)
            if action == 'no': await q.message.reply_text('Download cancelled.')
        elif action == 'movie':
            if getattr(self, 'discovery_tasks', None):
                await q.message.reply_text('A search is processing. Please wait until it finishes.')
                return
            await q.message.reply_text('Finding available files and checking sizes…')
            self.launch_discovery(self.safe_movie_card(context, key))
        elif action in ('file','retryfile'):
            stream = self.store.cached(key, 'unchecked' if action == 'retryfile' else 'stream')
            if not stream:
                await q.message.reply_text('File result expired. Use /search again.'); return
            if not (isinstance(stream.get('size'),(int,float)) and stream['size']>MAX_BYTES):
                if not await self.transfer_gate(q.message): return
            if getattr(self, 'discovery_tasks', None):
                await q.message.reply_text('Processing. Please wait.'); return
            self.launch_discovery(self.prepare_choice(context, stream))

    def file_page(self, selection, ids, page):
        page=max(0,min(page,max(0,(len(ids)-1)//4)))
        rows=[]
        for key in ids[page*4:page*4+4]:
            stream=self.store.cached(-key,'unchecked') if key < 0 else self.store.cached(key,'stream')
            if not stream: continue
            size=stream.get('size'); size_text=f'{size/1_000_000:.1f} MB' if isinstance(size,(int,float)) else 'size unknown'
            action=('Unavailable' if stream.get('check_status') == 'unavailable' else 'Retry') if key < 0 else ('Manual' if stream.get('check_status') == 'manual' else ('Link' if isinstance(size,(int,float)) and size>MAX_BYTES else ('DW' if size else 'Size unknown')))
            if action == 'DW' and self.transfer_busy(): action = 'Wait (busy)'
            quality=str(stream.get('quality') or stream.get('title') or stream.get('name') or 'quality unknown')[:65]
            rows.append([Button(f'{action} {quality} | {size_text} | {stream.get("file_type","unknown")}'[:120],callback_data=f'{"retryfile" if key < 0 else "file"}:{abs(key)}')])
        navigation=[]
        if page: navigation.append(Button('Previous',callback_data=f'page:{selection*1000+page-1}'))
        if (page+1)*4<len(ids):navigation.append(Button('More',callback_data=f'page:{selection*1000+page+1}'))
        if navigation: rows.append(navigation)
        rows.append([Button('Back', callback_data='searchagain'), Button('Cancel',callback_data=f'cancelpage:{selection}'), Button('Main menu', callback_data='menu:main')])
        return Markup(rows)

    async def prepare_choice(self, context, stream):
        try:
            info=await probe(stream['url'],stream.get('headers'))
            state=info.get('status','ready')
            if state != 'ready':
                retry = info.get('retry_after')
                hint = f' Retry after {retry}.' if retry else ''
                if state == 'manual':
                    stream['check_status']='manual';stream['check_reason']=info.get('reason')
                    self.store.cache('stream',stream)
                    await context.bot.send_message(self.admin_id, f"{info.get('reason','Manual download required')}.\n{stream['url']}",disable_web_page_preview=True)
                else:
                    await context.bot.send_message(self.admin_id,f"{info.get('reason','Link check failed')}.{hint} Search again to retry.")
                return
            size=info['size']
            if not size:
                await context.bot.send_message(self.admin_id,'File size is unknown. No download started.');return
            actual=info['filename'] if Path(info['filename']).suffix else stream.get('filename') or info['filename']
            stream=dict(stream,filename=actual,size=size,confirmed_size=size,check_status='ready')
            stream['file_type']=Path(actual).suffix.lstrip('.').lower() or stream.get('file_type','unknown')
            self.store.cache('stream',stream)
            if size>MAX_BYTES:
                await context.bot.send_message(self.admin_id,f"{clean_title(stream['movie'],stream)}\n{actual}\n{size/1_000_000:.1f} MB — above 2000 MB. Saved as a link. Download it manually.\n{stream['url']}",disable_web_page_preview=True)
                return
            key=self.store.cache('confirmation',stream)
            await context.bot.send_message(self.admin_id,f"Download this file?\nCatalogue: {clean_title(stream['movie'],stream)}\nActual file: {stream['filename']}\nSize: {size/1_000_000:.1f} MB\nFormat: {stream.get('file_type','unknown')}\nSource: {stream.get('provider','unknown')}",reply_markup=Markup([[Button('Yes',callback_data=f'yes:{key}'),Button('No',callback_data=f'no:{key}')], navigation()]))
        except Exception:
            await context.bot.send_message(self.admin_id,'Could not verify this source and its size. No download started. Try another link.')

    async def safe_movie_card(self, context, key):
        try:
            await self.movie_card(context, key)
        except Exception:
            await context.bot.send_message(self.admin_id, 'Could not fetch file results. Please try /search again.')

    async def download_choice(self, context, stream):
        url = stream['url']
        identity=stream.get('identity')
        duplicate=self.store.db.execute("SELECT jobs.id FROM jobs JOIN settings ON settings.key='job:' || jobs.id || ':identity' WHERE settings.value=? AND jobs.state NOT IN ('failed','cancelled') LIMIT 1",(identity,)).fetchone() if identity else None
        if duplicate or self.store.find_source(url):
            await context.bot.send_message(self.admin_id, 'This source is already recorded. Check pending uploads or saved links.'); return
        info=await probe(url, stream.get('headers'))
        size, content_type=info.get('size'), info.get('content_type','')
        if info.get('status','ready') != 'ready' or not size:
            await context.bot.send_message(self.admin_id, f"Link status changed: {info.get('reason','size unknown')}. Search again to retry."); return
        if stream.get('confirmed_size') and size != stream['confirmed_size']:
            await context.bot.send_message(self.admin_id, 'The source size changed. Select the file again to review it.'); return
        movie = stream['movie']
        quality = str(stream.get('quality') or stream.get('title') or 'unknown')[:80]
        suffix = Path(stream.get('filename') or urlparse(url).path).suffix.lower()
        if suffix not in ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v'): suffix = '.mp4'
        title = clean_title(movie,stream)
        filename = safe_name(title) + suffix
        job = self.store.create(url, title, filename, size)
        if identity: self.store.set_setting(f'job:{job}:identity',identity)
        self.store.set_setting(f'job:{job}:original',stream.get('filename') or filename)
        self.store.set_setting(f'job:{job}:headers', json.dumps(stream.get('headers') or {}))
        if size > MAX_BYTES or any(t in content_type.lower() for t in ('text/html', 'mpegurl', 'dash+xml')) or stream['file_type'] in ('hls', 'm3u8', 'mpd', 'page'):
            self.store.update(job, state='oversize' if size > MAX_BYTES else 'link_only')
            await context.bot.send_message(self.admin_id, 'Saved this version as a link in Saved links. It exceeds the limit or uses a source format not supported by the downloader yet.'); return
        self.store.update(job, state='downloading')
        self.active_description = f'Downloading #{job} · {filename}'
        folder = self.media_dir / str(job); folder.mkdir(exist_ok=True)
        try:
            artwork = movie.get('background') or movie.get('poster')
            if artwork:
                image = await landscape(artwork, small=True)
                path = folder / 'catalogue.jpg'; path.write_bytes(image.getvalue())
                self.store.update(job, thumbnail=str(path))
        except asyncio.CancelledError:
            self.store.update(job, state='cancelled', error='Cancelled')
            raise
        except Exception:
            pass
        await self.perform_download(context, job)

    async def inline_search(self, update, context):
        q = update.inline_query
        if not q or not self.is_admin(q.from_user.id):
            if q: await q.answer([], cache_time=0, is_personal=True)
            return
        query = q.query.strip()
        results = []
        if len(query) >= 3:
            try:
                movies, _ = await asyncio.wait_for(Discovery(self.store).search(query), 8)
                for movie in movies[:15]:
                    key = self.store.cache('movie', movie)
                    results.append(InlineQueryResultArticle(str(key), movie['name'],
                        description=str(movie.get('year') or movie.get('releaseInfo') or ''),
                        thumbnail_url=movie.get('background') or movie.get('poster'),
                        input_message_content=InputTextMessageContent(f'{movie["name"]}\n{movie.get("year") or movie.get("releaseInfo") or ""}'),
                        reply_markup=Markup([[Button('View available files', url=f'https://t.me/{context.bot.username}?start=movie_{key}')]])))
            except Exception:
                pass
        await q.answer(results, cache_time=0, is_personal=True)
