from telethon import TelegramClient


class SessionManager:
    """Own the user session; the Telegram application owns the bot session."""

    def __init__(self, data_dir, api_id, api_hash):
        self.user = TelegramClient(str(data_dir / 'user'), api_id, api_hash)

    async def start(self):
        await self.user.start()
        await self.user.get_dialogs()

    async def stop(self):
        await self.user.disconnect()
