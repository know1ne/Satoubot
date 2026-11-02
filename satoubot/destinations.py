from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telethon.utils import get_peer_id


class DestinationFunctions:
    async def destination_menu(self, context, job_id=0):
        rows=[]
        try:
            dialogs=await self.uploader.get_dialogs()
            for dialog in dialogs:
                entity=dialog.entity
                if not getattr(entity,'creator',False): continue
                chat_id=get_peer_id(entity)
                try:
                    member=await context.bot.get_chat_member(chat_id,context.bot.id)
                    if member.status!='administrator': continue
                    if getattr(entity,'broadcast',False) and not member.can_post_messages: continue
                except Exception:
                    continue
                key=self.store.cache('destination',{'target':chat_id,'job':job_id,'title':dialog.name})
                rows.append([Button(dialog.name[:80],callback_data=f'dest:{key}')])
        except Exception:
            await context.bot.send_message(self.admin_id,'Could not list your channels. Check the user session and try again.');return
        rows.append([Button('Cancel',callback_data='destcancel')])
        await context.bot.send_message(self.admin_id,'Select a channel or group you own. Only chats where the bot is an administrator are listed. Add the bot as admin and retry if yours is missing.',reply_markup=Markup(rows))

    async def select_destination(self, update, context):
        q=update.callback_query
        if q.data=='destcancel':
            await q.edit_message_reply_markup(reply_markup=None);return
        if q.data.startswith('choose_dest:'):
            try: job_id=int(q.data.split(':')[1])
            except ValueError:return
            await self.destination_menu(context,job_id);return
        try: key=int(q.data.split(':')[1])
        except ValueError:return
        destination=self.store.cached(key,'destination')
        if not destination:
            await q.message.reply_text('Selection expired. Open the channel menu again.');return
        member=await context.bot.get_chat_member(destination['target'],context.bot.id)
        if member.status!='administrator':
            await q.message.reply_text('Make the bot an administrator before selecting this chat.');return
        self.store.set_setting('target',destination['target'])
        await q.edit_message_reply_markup(reply_markup=None)
        job_id=destination['job']
        if job_id and self.store.get(job_id) and self.store.get(job_id)['state']=='review':
            await self.preview(context,job_id)
        else:
            await q.message.reply_text('Destination selected: '+destination['title'])
