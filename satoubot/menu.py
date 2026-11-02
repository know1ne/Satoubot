from types import SimpleNamespace
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup


def navigation():
    return [Button('Back', callback_data='menu:main'), Button('Cancel', callback_data='menu:cancel'), Button('Main menu', callback_data='menu:main')]


class MenuFunctions:
    def clear_prompt(self, context):
        for key in ('manifest_pending', 'uploaded_manifest', 'catalogue_search_pending', 'search_pending', 'admin_add_pending', 'thumbnail_request'):
            context.user_data.pop(key, None)

    async def main_menu(self, message):
        busy = self.transfer_busy()
        state = 'on' if self.transfers_enabled() else 'off'
        rows = [[Button('Download' + (' (busy)' if busy else ''), callback_data='menu:download'), Button('Upload approvals', callback_data='menu:pending')],
                [Button('Add-ons', callback_data='menu:addons'), Button('Saved links', callback_data='menu:saved')],
                [Button('Library search', callback_data='menu:library'), Button('My channels', callback_data='menu:channels')],
                [Button('Active transfers', callback_data='menu:active'), Button('Admin management', callback_data='menu:admins')],
                [Button('Stop transfers' if self.transfers_enabled() else 'Start transfers', callback_data='menu:toggle')]]
        await message.reply_text(f'Main menu · Transfers {state}\nSend a movie title to search catalogues.', reply_markup=Markup(rows))

    async def admin_menu(self, message, user_id):
        if user_id != self.owner_id:
            await message.reply_text('Only the owner can manage admins.', reply_markup=Markup([navigation()])); return
        ids = [row[0] for row in self.store.db.execute('SELECT user_id FROM admins ORDER BY user_id')]
        rows = [[Button(f'Remove {uid}', callback_data=f'adminremove:{uid}')] for uid in ids]
        rows += [[Button('Add admin', callback_data='menu:addadmin')], navigation()]
        await message.reply_text(f'Owner: {self.owner_id}\nAdmins: ' + (', '.join(map(str, ids)) or 'none'), reply_markup=Markup(rows))

    async def active_menu(self, message):
        tasks = [task for task in self.tasks if not task.done()]
        rows = [[Button('Refresh', callback_data='menu:active')]]
        if tasks: rows.append([Button('Cancel transfer', callback_data='menu:canceltransfer')])
        rows.append(navigation())
        label = getattr(self, 'active_description', 'Transfer in progress') if tasks else 'No active transfers.'
        await message.reply_text(label, reply_markup=Markup(rows))

    async def menu_callback(self, update, context):
        q=update.callback_query; action=q.data.split(':',1)[1]
        if q.data.startswith('adminremove:'):
            if update.effective_user.id != self.owner_id:
                await q.message.reply_text('Only the owner can manage admins.');return
            try: uid=int(action)
            except ValueError:return
            self.store.db.execute('DELETE FROM admins WHERE user_id=?',(uid,));self.store.db.commit()
            await self.admin_menu(q.message, update.effective_user.id);return
        if action in ('main','cancel'):
            self.clear_prompt(context)
            await self.main_menu(q.message);return
        if action == 'active': return await self.active_menu(q.message)
        if action == 'canceltransfer':
            active=[t for t in self.tasks if not t.done()]
            if active:
                for task in active: task.cancel()
                await q.message.reply_text('Cancellation requested. An upload may already have reached Telegram; check the destination before retrying.')
            else: await q.message.reply_text('No active transfers.')
            return await self.active_menu(q.message)
        if action == 'admins': return await self.admin_menu(q.message, update.effective_user.id)
        if action == 'addadmin':
            if update.effective_user.id != self.owner_id:
                await q.message.reply_text('Only the owner can manage admins.');return
            self.clear_prompt(context);context.user_data['admin_add_pending']=True
            await q.message.reply_text('Send the numeric Telegram user ID.',reply_markup=Markup([navigation()]));return
        if action == 'toggle':
            self.store.set_setting('transfers_enabled','0' if self.transfers_enabled() else '1')
            return await self.main_menu(q.message)
        self.clear_prompt(context)
        proxy=SimpleNamespace(message=q.message,effective_user=update.effective_user,effective_chat=update.effective_chat)
        if action == 'download':
            text='A transfer is active. Wait before starting another.' if self.transfer_busy() else 'Send a direct HTTP(S) media link.'
            await q.message.reply_text(text,reply_markup=Markup([navigation()]));return
        if action == 'library':
            context.user_data['search_pending']=True
            await q.message.reply_text('Send a title to search your published library.',reply_markup=Markup([navigation()]));return
        await q.message.reply_text('Choose an action or return to the main menu.',reply_markup=Markup([navigation()]))
        if action == 'addons': return await self.addons(proxy,context)
        if action == 'saved': return await self.saved(proxy,context)
        if action == 'pending': return await self.pending(proxy,context)
        if action == 'channels': return await self.destination_menu(context)
