<!-- coderabbit-ai-generated:v1 -->

# SatouBot

Early foundation for a private Telegram media administrator. This is not yet the complete bot.

## Current functionality

A single configured admin can select a channel or group, send a direct HTTP(S) media URL, inspect its reported size, download a file within the 2000 MB limit, rename it, and approve an upload. The upload uses the logged-in Telegram user account through MTProto, so the post is sent by that account. The selected account needs access to the destination. File transfers report progress, speed, and estimated time. `/status` shows CPU and memory, `/find` searches locally recorded published items, and `/saved` lists oversized links.

A download uses up to four HTTP byte-range connections when the server advertises range support and provides a stable validator. Otherwise it uses a single connection. Speed depends on the source and network. The MTProto uploader currently uses Telethon's standard uploader; parallel upload acceleration is not implemented.

## Setup

Use Python 3.11 or newer. Create a Telegram bot with BotFather and obtain an API ID and hash from Telegram. Start a [local Bot API server](https://core.telegram.org/bots/api#using-a-local-bot-api-server) separately. Configure it for local mode.

1. Create a virtual environment: `python -m venv .venv` and activate it.
2. Install dependencies: `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and fill in the bot token, your numeric Telegram user ID, API ID, API hash and local Bot API address.
4. Run `python -m satoubot.app`. On first run, authenticate the user account in the local terminal. Never share the session file, login codes or `.env`.
5. Add the bot and user account to the destination with appropriate permissions. Start the bot in a private chat and select the destination.

Keep the data directory private; it contains the user session, database, saved source URLs and downloaded files. A published item with an unknown upload outcome needs manual inspection before retrying.

## Work remaining

Catalogue and addon discovery, automatic daily discovery, quality choices, thumbnail management, inline file search, migration, source refresh, torrent and M3U8 downloads, download-page extraction, automated error retries and channel availability monitoring are not implemented yet. Reported links are recorded but not automatically refreshed. The current search records message IDs and does not deliver a Telegram file. Do not rely on this foundation for unattended publishing or migration.

## Testing

Install pytest and pytest-asyncio and run `python -m pytest -q`. Tests do not require Telegram credentials. Live Telegram transfers require a real bot, account and destination.
