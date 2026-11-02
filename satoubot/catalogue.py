import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse

from telegram import (InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup,
                      InlineQueryResultArticle, InputTextMessageContent)
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
            reply_markup=Markup([[Button('Add Yoru providers', callback_data='defaultproviders')]]))

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
            text = 'Choose a movie to see its image, details and available files. No download starts until you select one.' if buttons else 'No matching movies found.'
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
        streams, errors = await service.streams(movie)
        semaphore = asyncio.Semaphore(4)
        async def inspect(stream):
            async with semaphore:
                stream['size_verified'] = False
                stream['file_type'] = Path(stream.get('filename') or urlparse(stream['url']).path).suffix.lstrip('.').lower() or 'unknown'
                headers = stream.get('headers') or {}
                stream['headers'] = {str(k): str(v) for k,v in headers.items()} if isinstance(headers, dict) else {}
                try:
                    info = await asyncio.wait_for(probe(stream['url'], stream['headers']), 8)
                    size, content_type = info['size'], info['content_type']
                    stream['filename'] = info['filename'] if Path(info['filename']).suffix.lower() in ('.mkv','.mp4','.avi','.webm','.mov') else stream.get('filename') or info['filename']
                    stream['file_type'] = 'hls' if 'mpegurl' in content_type.lower() else stream['file_type']
                    stream['file_type'] = 'page' if 'text/html' in content_type.lower() else stream['file_type']
                    stream['size'] = size
                    stream['size_verified'] = True
                    if stream['file_type'] in ('hls', 'm3u8', 'mpd', 'page') or 'dash+xml' in content_type.lower():
                        stream['size'] = None
                        stream['size_verified'] = False
                except Exception:
                    pass
        await asyncio.gather(*(inspect(s) for s in streams))
        buttons, lines, source_ids = [], [], []
        for index, stream in enumerate(streams):
            stream['movie'] = movie
            if stream.get('size_verified') and stream.get('size') and Path(stream.get('filename') or '').suffix.lower() in ('.mp4','.mkv','.avi','.mov','.webm'):
                stream['identity'] = movie['id'] + ':' + stream['filename'].casefold() + ':' + str(stream['size'])
            key = self.store.cache('stream', stream)
            if key in source_ids: continue
            source_ids.append(key)
            quality = str(stream.get('quality') or stream.get('title') or stream.get('name') or 'Quality unknown')[:70]
            size = stream.get('size')
            size_label = f'{size/1_000_000:.1f} MB' if isinstance(size, (float, int)) and size > 0 else 'size unknown'
            file_type = stream['file_type']
            label = f'{index+1}. {quality} | {size_label} | {file_type}'
            lines.append(label)
            link_only = isinstance(size, (float, int)) and size > MAX_BYTES or file_type in ('hls', 'm3u8', 'mpd', 'page')
            action = 'Save link' if link_only else 'Download'
            buttons.append([Button(f'{action}: {label}'[:120], callback_data=f'file:{key}')])
        qualities = ', '.join(dict.fromkeys(str(s.get('quality') or 'unspecified')[:35] for s in streams))[:250]
        caption = f'{movie["name"][:150]}\nYear: {movie.get("year") or movie.get("releaseInfo") or "unknown"}\nQualities: {qualities or "none found"}\nAvailable files: {len(source_ids)}\nSelect a file to download. Larger or unsupported files can be saved as links.'
        if errors: caption += f'\n{len(errors)} source(s) failed or need configuration.'
        if not streams: caption += '\nNo playable links returned. Try another source or movie.'
        artwork = movie.get('background') or movie.get('poster')
        selection = self.store.cache('pages', source_ids)
        markup = self.file_page(selection, source_ids, 0)
        try:
            if not artwork: raise ValueError()
            photo = await landscape(artwork)
            await context.bot.send_photo(self.admin_id, photo=photo, caption=caption, reply_markup=markup)
        except Exception:
            await context.bot.send_message(self.admin_id, caption + '\nImage unavailable.', reply_markup=markup)

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
            await q.edit_message_reply_markup(reply_markup=self.file_page(selection, ids, page))
        elif action == 'cancelpage':
            self.store.db.execute("DELETE FROM discovery_cache WHERE id=? AND kind='pages'",(key,));self.store.db.commit()
            await q.edit_message_reply_markup(reply_markup=None)
        elif action in ('yes', 'no'):
            stream = self.store.cached(key, 'confirmation')
            if not stream:
                await q.message.reply_text('This confirmation expired or was already used.'); return
            if action == 'yes' and not await self.transfer_gate(q.message): return
            self.store.db.execute("DELETE FROM discovery_cache WHERE id=? AND kind='confirmation'",(key,));self.store.db.commit()
            await q.edit_message_reply_markup(reply_markup=None)
            if action == 'yes': self.spawn(self.download_choice(context, stream))
            else: await q.message.reply_text('Download cancelled.')
        elif action == 'movie':
            if getattr(self, 'discovery_tasks', None):
                await q.message.reply_text('A search is processing. Please wait until it finishes.')
                return
            await q.message.reply_text('Finding available files and checking sizes…')
            self.launch_discovery(self.safe_movie_card(context, key))
        elif action == 'file':
            stream = self.store.cached(key, 'stream')
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
            stream=self.store.cached(key,'stream')
            if not stream: continue
            size=stream.get('size'); size_text=f'{size/1_000_000:.1f} MB' if isinstance(size,(int,float)) else 'size unknown'
            action='Link' if isinstance(size,(int,float)) and size>MAX_BYTES else 'DW'
            quality=str(stream.get('quality') or stream.get('title') or stream.get('name') or 'quality unknown')[:65]
            rows.append([Button(f'{action} {quality} | {size_text} | {stream.get("file_type","unknown")}'[:120],callback_data=f'file:{key}')])
        navigation=[]
        if page: navigation.append(Button('Previous',callback_data=f'page:{selection*1000+page-1}'))
        if (page+1)*4<len(ids):navigation.append(Button('More',callback_data=f'page:{selection*1000+page+1}'))
        if navigation: rows.append(navigation)
        rows.append([Button('Cancel',callback_data=f'cancelpage:{selection}')])
        return Markup(rows)

    async def prepare_choice(self, context, stream):
        reported=stream.get('size')
        if isinstance(reported,(int,float)) and reported>MAX_BYTES:
            self.store.cache('stream',stream)
            await context.bot.send_message(self.admin_id,f"{clean_title(stream['movie'],stream)}\n{stream.get('filename') or 'Filename not supplied'}\n{reported/1_000_000:.1f} MB — above 2000 MB. Saved as a link. Download it manually.\n{stream['url']}",disable_web_page_preview=True)
            return
        try:
            info=await probe(stream['url'],stream.get('headers'))
            size=info['size']
            if not size: raise ValueError()
            actual=info['filename'] if Path(info['filename']).suffix else stream.get('filename') or info['filename']
            stream=dict(stream,filename=actual,size=size,confirmed_size=size)
            self.store.cache('stream',stream)
            if size>MAX_BYTES:
                await context.bot.send_message(self.admin_id,f"{clean_title(stream['movie'],stream)}\n{info['filename']}\n{size/1_000_000:.1f} MB — above 2000 MB. Saved as a link. Download it manually.\n{stream['url']}",disable_web_page_preview=True)
                return
            key=self.store.cache('confirmation',stream)
            await context.bot.send_message(self.admin_id,f"Download this file?\nCatalogue: {clean_title(stream['movie'],stream)}\nActual file: {stream['filename']}\nSize: {size/1_000_000:.1f} MB\nFormat: {stream.get('file_type','unknown')}\nSource: {stream.get('provider','unknown')}",reply_markup=Markup([[Button('Yes',callback_data=f'yes:{key}'),Button('No',callback_data=f'no:{key}')]]))
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
        try:
            size, content_type = await inspect_url(url, stream.get('headers'))
        except Exception:
            await context.bot.send_message(self.admin_id, 'Could not verify the file size. No download started. Try another version.'); return
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
        folder = self.media_dir / str(job); folder.mkdir(exist_ok=True)
        try:
            artwork = movie.get('background') or movie.get('poster')
            if artwork:
                image = await landscape(artwork, small=True)
                path = folder / 'catalogue.jpg'; path.write_bytes(image.getvalue())
                self.store.update(job, thumbnail=str(path))
        except Exception:
            pass
        self.store.update(job, state='downloading')
        await self.perform_download(context, job)

    async def inline_search(self, update, context):
        q = update.inline_query
        if not q or q.from_user.id != self.admin_id:
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
