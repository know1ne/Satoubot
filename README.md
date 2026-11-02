<!-- coderabbit-ai-generated:v1 -->

# SatouBot

Early foundation for a private Telegram media administrator. This is not yet the complete bot.

## Current functionality

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:87948aaa984bf25a9e6ce9d14879ae6030f10981f89d51340fc7beba15996bf7 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:cd08f449df7a869c6b60d71cde3044f59fd70464fd10e392552c1f10ad0ecdac -->

The configured owner and any admins the owner adds can select a channel or group, send a direct HTTP(S) media URL, check it, download a file within the 2000 MB limit, rename it, choose a thumbnail, and approve an upload.

<!-- /coderabbit-ai-generated:v1 --> On a review preview, choose **Set thumbnail** and send a photo; review the new preview before approving. Telegram may ignore custom thumbnails for some file types or media without recognizable video dimensions. The upload uses the logged-in Telegram user account through MTProto, so the post is sent by that account. The selected account needs access to the destination. File transfers report progress, speed, and estimated time. `/status` shows CPU and memory, `/find` searches locally recorded published items, and `/saved` lists oversized links.

<!-- /coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:2adf38604fc3a0c26566982025949205956355cc4286313fa64c92dec101169b -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:007bc17cda87143c84e1510467b74f3ab77596cf5107e8832ece0ddbd6f4737d -->

A download uses up to four HTTP byte-range connections when the server advertises range support and provides a stable validator. No aria2 integration or transcoding is used; the verified filename supplies the displayed format, and ffprobe is not required.

<!-- /coderabbit-ai-generated:v1 --> Otherwise it uses a single connection. Before saving and before downloading, the bot checks sources using a small byte range, validates public redirect destinations and reports unavailable, blocked, temporary and manual-only sources. It stops if a source ignores the range. Sources with unverified size cannot be downloaded. Download and upload progress messages show a progress bar, size, speed and ETA. Speed depends on the source and network. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:5c6486be6df86ece30017036307bec0b5962e0051a14f003b07fa5197b195b4f -->

The MTProto uploader uses Telethon's standard uploader by default. An experimental option sends several file parts concurrently through the same user session; see **Experimental upload speed** below.

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:049204d5e1a86d67e82eff8a07b157d70779dcc55fdbf06bff1f16d8fe287ff8 -->

## Transfer controls and menu

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:c1e59b9411bb3c153bf6f6bab780accf4529ece171cfb3015681456d2efe2204 -->

Send `/start` to remove the old bottom keyboard and open the inline menu. Send a movie title in the private chat to search. Use **Admin management** to list, add or remove numeric Telegram user IDs; only the configured owner can change admins, and the list is stored in the database. Each admin must start a private chat with the bot.

Use **Start transfers** and **Stop transfers** to allow or block new downloads and uploads. The setting persists across restarts. One transfer runs at a time, and menus and searches remain usable during it. **Active transfers** shows and cancels the current transfer. A cancelled or failed upload may already be present on Telegram: inspect the destination before retrying. A running transfer finishes when transfers are switched off. Uploads still require individual approval.

<!-- /coderabbit-ai-generated:v1 --> <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:de2ef4aa517506ef486208025498897c72c699345fbdfbeeb4d9425d402ff666 -->

Group management and migration menu entries are placeholders for later features. My channels lists channels and groups owned by the logged-in user where the bot is an administrator. When no destination is set, upload review opens this selection; selecting a destination generates a fresh approval preview. Bot administrator/posting access is checked again before upload.

<!-- /coderabbit-ai-generated:v1 -->

Download, upload, search, group management, migration and user session code are in separate modules. The bot session is managed by the Telegram application; the user session is stored locally. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:cf34a90fefbf7919e4ed1107ac01d867678b19c86c155b6e0980465019350997 -->

Movie-name search and automatic catalogue artwork are available through supported Stremio catalogue manifests. Scheduled daily discovery is still pending.

<!-- /coderabbit-ai-generated:v1 -->

## Setup

<!-- /coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:5bc732f79730278c318642c0029ca9f77c19895ebaa59fd8097b948319bde880 -->

### Install on a PC (Ubuntu or Windows with WSL)

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:db7a4107844e90140305ce902e729fd0d3364c8b225d4a29fecdcbb0286996d3 -->

These steps run the bot on Ubuntu. On Windows, first install [WSL with Ubuntu](https://learn.microsoft.com/windows/wsl/install) and run the commands in the Ubuntu terminal. Install Python 3.11 or newer, Git, Node.js 24 or newer and npm first. Check with `python3 --version`, `git --version`, `node --version` and `npm --version`.

Install Bubblewrap with `sudo apt update && sudo apt install bubblewrap`. Nuvio plugins need working unprivileged user and network namespaces; the bot fails closed for plugins if Bubblewrap or those namespaces are unavailable. Catalogue add-on lookups work independently.

<!-- /coderabbit-ai-generated:v1 -->

1. Get this repository and enter its folder. To use this task's changes, first publish the task branch, then check it out:

   ```bash
   git clone https://github.com/cinespicebusinesss/Satoubot.git
   cd Satoubot
   git switch coderabbit/telegram-media-admin-bot/de70788f
   ```

2. Install bot dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   npm ci --prefix satoubot/plugin_runtime
   cp .env.example .env
   ```

   On Ubuntu, install the matching `python3-venv` package if creating the environment fails. Keep the terminal open.

3. Create a bot with [BotFather](https://t.me/BotFather). Get your numeric Telegram user ID, and obtain your own API ID and API hash from [my.telegram.org](https://my.telegram.org). Edit `.env` (for example `nano .env`) and set `BOT_TOKEN`, `ADMIN_USER_ID`, `TELEGRAM_API_ID`, and `TELEGRAM_API_HASH`. Leave `BOT_API_URL=http://127.0.0.1:8081` for a server on the same machine. `DATA_DIR=./data` stores downloads in the repository's `data/media` directory. Keep `.env` and the `data` directory private.

4. Install Telegram's **local Bot API server** separately using the [official build instructions generator](https://tdlib.github.io/telegram-bot-api/build.html) for Ubuntu. It requires a compiler, CMake, gperf, OpenSSL and zlib. In a second terminal, from the SatouBot folder, load the credentials and start the installed server:

   ```bash
   set -a
   source .env
   set +a
   telegram-bot-api --local
   ```

   Keep this terminal running. The server uses `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from the environment and listens on port 8081 by default. If the executable was installed outside your PATH, use its full path. If this bot was previously connected to Telegram's hosted Bot API, follow [Telegram's local-server switching instructions](https://github.com/tdlib/telegram-bot-api#switching) before starting it; moving an existing bot logs it out of the previous API server.

5. In the first terminal, start the bot:

   ```bash
   source .venv/bin/activate
   python -m satoubot.app
   ```

   On first launch enter the Telegram account's phone number, login code, and two-step verification password if requested. The bot uses this user account for uploads. Keep both terminals running; stopping either stops its service. Restart the local server and bot with the same commands after restarting the PC.

6. Add the bot as an administrator to the destination channel or group, with posting permission, and ensure the user account can post there. Open a private chat with the bot and send `/start`. Use **Add-ons** to add a searchable catalogue, such as `https://v3-cinemeta.strem.io/manifest.json`; see **Catalogue and provider search** below. To use inline search, enable it via BotFather's `/setinline`.

This foundation is not configured to start automatically with the PC. A published item with an unknown upload outcome needs manual inspection before retrying.

<!-- /coderabbit-ai-generated:v1 -->

## Work remaining

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:d9d2894ba7cd912ffc037573d9224c035c444c7fc48121c5c8c1206f663dd303 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:bcf4cec059eae3e6359b3e9dc943e80d96947af0988b3bb6a1fd372c18e336fa -->

Automatic daily discovery, inline retrieval of already published Telegram files, migration, source refresh, torrent, DASH and M3U8 downloads, generic download-page extraction, automated error retries and channel availability monitoring are not implemented yet. Search can list HTTP links returned by providers; unsupported formats and files above 2000 MB are saved as links. A size that cannot be verified prevents downloading. Provider success depends on source availability and compatibility; plugin settings screens are not implemented.

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 --> Reported links are recorded but not automatically refreshed. The current search records message IDs and does not deliver a Telegram file. Do not rely on this foundation for unattended publishing or migration.

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:2b768b0766e5fdb057113593cd372e2670be07e86850e9ba5177c61f706d0cb4 -->

## Catalogue and provider search

1. Install Node.js 24+ and run `npm ci --prefix satoubot/plugin_runtime` once, alongside the Python dependencies.
2. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:fb37879783d815423664c9757cbb778cdcd0d4db283cf72347493eb253154107 -->

Open **Add-ons** (or `/addons`) and send a Stremio catalogue manifest URL, or upload a JSON manifest and then provide its original URL so relative endpoints can be resolved.

<!-- /coderabbit-ai-generated:v1 --> For example, Cinemeta: `https://v3-cinemeta.strem.io/manifest.json`. The catalogue must advertise a movie catalogue with a `search` extra. For Nuvio plugins, movie metadata must include `moviedb_id`, `tmdb_id`, or a `tmdb:` ID.
3. Add a Nuvio repository URL in the same menu, or use **Add Yoru providers**. The supplied Yoru repository is loaded automatically on the first source lookup if no Nuvio repository has been saved. Manifests persist in the database. Provider scripts are fetched when looking up streams.
<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:8619b6da6c26c4ffaea9d24c3732550a9255337a3f2ce19ac84fd333b501d1d2 -->

4. Send a movie name or `/search Insidious`. Select the correct movie; the bot checks sources concurrently and shows checked files as they become available, with landscape artwork where available. Source lookup uses at most four concurrent providers, a 30-second per-provider timeout and a 90-second overall timeout. Add-ons on the same host are queried one at a time. Manifest and movie metadata responses are cached in memory for 60 seconds; file availability and access are still checked before downloads. A size labelled unknown is rechecked before download. Search alone never downloads or uploads media.

<!-- /coderabbit-ai-generated:v1 -->
5. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:f170a0d2386266b1c93feccf8ae6db1313ece5396e76a4191a369342198d36de -->

File choices show four per page with More/Previous and Cancel. Select a **DW** button to see catalogue details, the actual source filename and verified size, then choose Yes or No. Only Yes starts a download. A separate approval is required before upload. Downloads show the actual filename, downloaded/total bytes, speed and ETA every five seconds, including during stalls, and send a completion notice. The local file is then renamed using the catalogue title, year, detected quality/codec and language labels. Source headers and alternative URLs are retained. Versions above 2000 MB return a saved link for manual download and never enter the download/upload path.

<!-- /coderabbit-ai-generated:v1 -->
6. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:47886b2bd16ef26c9b0fd1206b19ff93c9818d3878a30600dd0adf62ca036a75 -->

**Library search** and `/find` search previously published records. Send a movie title or use `/search` to search online catalogues. Both work with transfers off.

<!-- /coderabbit-ai-generated:v1 -->

For Telegram inline search, enable inline mode for the bot through BotFather (`/setinline`). Type `@your_bot_username Insidious` in Telegram. Results include a **View available files** button that opens the private bot. Inline results are restricted to the configured admin; <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:4f23426e260a7d8dd36c8fe123c33368ac37f5cfc727865f06d76e384bb78b7b -->

movie, page and confirmation selections expire after one hour; source URLs and file information are stored persistently in the database and shown in Saved links. Identical URLs are merged; matching movie IDs, verified source filenames and byte sizes merge known mirror choices. Files with different names and no shared content identity cannot be proven identical before download.

<!-- /coderabbit-ai-generated:v1 --> `/search` is the private-chat command.

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:5d5fc0435d3fef8777d721187bf6dec0d15c88451972e3bfdcb33c8a7337f4e7 -->

Nuvio scripts run in a Bubblewrap-isolated Node process with a private network namespace, no host user files, a cleared environment and resource limits. Node permissions and `node:vm` do not provide isolation themselves. Plugin HTTP requests pass through a broker that permits public destinations only, pins DNS results and checks redirects individually. Private network plugin destinations are unavailable. Bubblewrap and working unprivileged namespaces are required; plugin execution fails closed if they are unavailable. Only add repositories you trust. The broker restriction applies to plugin requests; other add-on and media requests use their existing network paths. The compatibility bridge provides fetch, timers, URL utilities, Cheerio, CryptoJS and empty `SCRAPER_SETTINGS`.

<!-- /coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:611db00e8b6213770324c61d70aa7ed403c86b4e5ca47895b0f6508470e28849 -->

## Experimental upload speed

The bot already uploads using your Telegram user account. To try sending several large-file parts concurrently, set `UPLOAD_PARALLEL_PARTS=4` in `.env` and restart the Python bot. Values `2` through `4` enable the experiment for files larger than 10 MiB; `1` (the default) uses the standard uploader. These are concurrent requests on the existing user session, not four separate TCP connections. Smaller files use the standard uploader.

Compare the progress speeds on similarly sized uploads on your own connection. Actual speed depends on the connection and Telegram; no improvement is guaranteed and the experimental mode has not been tested against live Telegram. If it fails or performs worse, set the value back to `1` and restart. If an upload outcome is unknown, check the destination before trying again. The experiment preserves the separate upload approval and does not automatically retry a publication.

## Testing

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 -->

Install pytest and pytest-asyncio and run `python -m pytest -q`. Tests do not require Telegram credentials. Live Telegram transfers require a real bot, account and destination.
