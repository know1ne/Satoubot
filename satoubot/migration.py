from telegram import InlineKeyboardMarkup as Markup
from .menu import navigation


class MigrationFunctions:
    async def migration_menu(self, update, context):
        if not self.authorized(update):
            return
        await self.show_panel(
            update.message,
            'Migration\nChannel/group migration is awaiting the source and destination workflow. '
            'No messages will be copied or deleted until that flow is configured.',
            reply_markup=Markup([navigation()]),
        )
