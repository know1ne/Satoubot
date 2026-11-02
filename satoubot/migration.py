class MigrationFunctions:
    async def migration_menu(self, update, context):
        await update.message.reply_text("Migration is planned for a later feature.")
