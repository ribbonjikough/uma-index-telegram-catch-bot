# Uma-Index Telegram Bot

Uma-Index Telegram Bot is a Telegram bot that monitors [uma-index.top](https://www.uma-index.top/?mode=twitter) and posts the English-translated Uma Musume comics to every user, group and channel that subscribed to it. Each post comes with a "Done reading" button that deletes the comic from the chat once it has been read.

> **Try it live on Telegram:** [**@angusbeefsteakbot**](https://t.me/angusbeefsteakbot) — send `/start` to see the bot in action without hosting it yourself!

---

## Features

- Scans a page window on the site (defaults to pages `1..3`, set with `MIN_PAGES` and `MAX_PAGES`) and sends every translated page at full resolution from oldest to newest.
- Catches up automatically: posts missed while the bot was offline are sent on the next run, and nothing is ever sent twice (tracked per chat in SQLite).
- Works in private chats, groups and channels. Add the bot to a chat and it subscribes itself.
- Paced sending that respects Telegram rate limits, with automatic retry on flood-wait.
- Colored terminal logs (timestamp and level only), with the bot token redacted.

---

## Setup and Installation

### Prerequisites
- **Python 3.10+**
- Windows for the `.bat` helpers (macOS and Linux users can follow the manual steps below).

### 1. Clone the Repository
```bash
git clone https://github.com/ribbonjikough/uma-index-telegram-bot.git
cd uma-index-telegram-bot
```

### 2. Create and Activate a Virtual Environment
```bash
python -m venv .venv

# On Windows:
.venv\Scripts\activate

# On Mac/Linux:
source .venv/bin/activate
```

### 3. Install Python Dependencies
```bash
pip install -r requirements.txt
```
This is the same as running:
```bash
pip install "python-telegram-bot[job-queue]>=21.6" playwright httpx python-dotenv
```

### 4. Install the Browser
The bot reads the site with a headless Chromium, installed once through Playwright:
```bash
playwright install chromium
```

### 5. Get a Telegram Bot Token
1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the instructions to name your bot.
3. Copy the token BotFather gives you (for example `123456789:ABCdefGHI...`).

### 6. Environment Configuration
Copy the example file and fill it in:
```bash
cp .env.example .env
```
```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=
MIN_PAGES=1
MAX_PAGES=3
```

| Variable | Required | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Yes | Token from @BotFather. |
| `TELEGRAM_CHAT_ID` | No | A fixed channel or chat to always post to. Leave empty to post only to chats that subscribed. |
| `MIN_PAGES` | No | Lower bound of the page scan window. Default is `1`. |
| `MAX_PAGES` | No | Upper bound of the page scan window. Default is `3`. |

The bot scans inclusively between `MIN_PAGES` and `MAX_PAGES` and posts matched comics in oldest-to-newest order.

### 7. Run the Bot
```bash
python bot.py
```
Open your bot in Telegram, send `/start`, and the comics you have not received yet will arrive shortly.

---

## Windows Shortcuts (⚠️RUN AT YOUR OWN RISKS⚠️)

- `setup.bat` creates `.venv`, installs the requirements and installs Chromium (RUN ONLY ONCE).
- `autorun.bat` goes to its own folder, activates `.venv` and starts the bot. Double-click it, or put a shortcut in the Startup folder to launch the bot at login.

---

## Bot Commands

| Command | What it does |
| --- | --- |
| `/start` | Subscribes the chat and sends any posts it has not received yet. |
| `/stop` | Pauses delivery. History is kept, so no duplicates on `/start`. |
| `/status` | Shows sync state, delivered count and subscriber count. |
| `/forcecheck` | Scans the site immediately instead of waiting for the next poll. |

To post in a channel or group, add the bot as an **administrator** with the "Post messages" and "Delete messages" permissions. The "Done reading" button cannot delete messages older than 48 hours.

---

## Notes

- Keep `.env` private and never commit it. It is already listed in `.gitignore`.
- The bot creates `uma_seen.db` on first run. Delete it to reset all delivery history, remove it from .gitignore if you want to keep all the data and history across multiple servers/devices.
- This is an unofficial fan tool. Please be considerate to the source site and keep the poll interval reasonable.

---

## Live Bot & Disclaimer

Want to read the translated comics right away without setting up Python? Check out the live instance running on Telegram at [**@angusbeefsteakbot**](https://t.me/angusbeefsteakbot).

> **Disclaimer:** This is an unofficial, fan-made utility created for indexing and reading translated *Uma Musume: Pretty Derby* comics on Telegram. This project is not affiliated with Cygames, X/Twitter, or [uma-index.top](https://www.uma-index.top/). All original artwork belongs to the respective artists, and all translations belong to the respective translators credited on each post and [@EDemocraft](https://x.com/EDemocraft) for the coding of the [website](https://www.uma-index.top/).

### Final words
Contributions, forks or issues are welcome!