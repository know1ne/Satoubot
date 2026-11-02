from telegram import InlineKeyboardButton, InlineKeyboardMarkup

class SearchFunctions:
    async def saved(self, update, context):
        if not self.authorized(update): return
        rows = self.store.db.execute("SELECT * FROM jobs WHERE state IN ('oversize','reported','link_only') ORDER BY id DESC LIMIT 20").fetchall()
        sources=self.store.db.execute('SELECT id,payload FROM source_links ORDER BY id DESC LIMIT 20').fetchall()
        if not rows and not sources:
            await update.message.reply_text('No saved links.'); return
        for row in rows:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Report dead link', callback_data=f'report:{row["id"]}')]])
            await update.message.reply_text(f'#{row["id"]} {row["filename"]} · {row["size"]/1_000_000:.1f} MB\n{row["source"]}', reply_markup=keyboard, disable_web_page_preview=True)

        import json
        for source in sources:
            item=json.loads(source['payload'])
            movie=item.get('movie',{})
            size=item.get('size')
            label=f"{movie.get('name','File')} | {item.get('quality') or 'quality unknown'} | {str(round(size/1_000_000,1))+' MB' if isinstance(size,(int,float)) else 'size unknown'}"
            await update.message.reply_text(label,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('View file / link',callback_data=f'file:{source["id"]}')]]))

    async def find(self, update, context):
        if not self.authorized(update): return
        query = ' '.join(context.args).strip()
        await self.search_query(update, query)

    async def search_query(self, update, query):
        if not query:
            await update.message.reply_text('Use /find <title>.'); return
        rows = self.store.search(query)
        text = '\n'.join(f'#{r["id"]} {r["title"]} · {r["size"]/1_000_000:.1f} MB · message {r["telegram_message_id"]}' for r in rows)
        await update.message.reply_text(text or 'No published matches found.')
