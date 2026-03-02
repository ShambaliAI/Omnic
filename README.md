# Minimal Telegram Bot (uv + Python)

This is the smallest usable verification version:

- `/start` returns a fixed message to confirm the bot is online.
- Regular text messages are persisted into SQLite for summary.

## Run

Make sure your `.env` contains:

```env
BOT_TOKEN=your_telegram_bot_token
SQLITE_PATH=omnic.sqlite3
OPENAI_API_KEY=your_openai_api_key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_CHAT_MODEL=gpt-4.1-mini
LOG_LEVEL=INFO
```

Start the bot:

```bash
uv run python main.py
```

On startup, the bot sends a test request to OpenAI Chat API.
If validation passes, it sends a readiness notice to known group chats in SQLite.
If validation fails (including quota-related errors), it sends a failure notice and exits with an exception.

## Verify

Send the following messages to your bot in Telegram:

1. `/start`
2. Any text, for example `ping`
3. `/summary 20`
4. `/summary 20 focus on decisions and blockers`
5. `/summary start`
6. Send several chat messages
7. `/summary end`
8. `/summary end focus on action items only`
9. `/summary usage`

Expected behavior:

- `/start` returns the `Bot is alive...` message.
- Text messages are recorded for summary and no echo message is sent.
- `/summary <count>` summarizes the latest `<count>` messages (max 1000).
- `/summary <count> [prompt]` supports extra customization prompt on top of default system prompt.
- `/summary start` stores per-user summary start position in SQLite.
- `/summary end` summarizes messages since that user's start position and clears it.
- `/summary end [prompt]` supports extra customization prompt on top of default system prompt.
- `/summary usage` shows used and remaining context count for the current user's start point.
