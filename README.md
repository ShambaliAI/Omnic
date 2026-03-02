# Minimal Telegram Bot (uv + Python)

This is the smallest usable verification version with two features:

- `/start` returns a fixed message to confirm the bot is online.
- Regular text messages are echoed back as-is.

## Run

Make sure your `.env` contains:

```env
BOT_TOKEN=your_telegram_bot_token
```

Start the bot:

```bash
uv run python main.py
```

## Verify

Send the following messages to your bot in Telegram:

1. `/start`
2. Any text, for example `ping`

Expected behavior:

- `/start` returns the `Bot is alive...` message.
- Text messages are echoed back unchanged.
