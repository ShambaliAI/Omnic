import os
import logging

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from db import connect_sqlite, record_message_event, setup_echo_feature

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    output_text = "Bot is alive. Send me any text and I will echo it."
    logger.debug(
        "input: /start | chat_id=%s user_id=%s",
        update.effective_chat.id if update.effective_chat else None,
        update.effective_user.id if update.effective_user else None,
    )
    logger.debug(
        "output: %r | chat_id=%s user_id=%s",
        output_text,
        update.effective_chat.id if update.effective_chat else None,
        update.effective_user.id if update.effective_user else None,
    )
    await update.message.reply_text(output_text)


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or update.message.text is None:
        return

    input_text = update.message.text
    output_text = input_text

    db_conn = context.application.bot_data.get("db_conn")
    if db_conn is not None:
        record_message_event(
            db_conn,
            update.effective_chat.id if update.effective_chat else None,
            update.effective_user.id if update.effective_user else None,
            input_text,
        )

    logger.debug(
        "input: %r | chat_id=%s user_id=%s",
        input_text,
        update.effective_chat.id if update.effective_chat else None,
        update.effective_user.id if update.effective_user else None,
    )
    logger.debug(
        "output: %r | chat_id=%s user_id=%s",
        output_text,
        update.effective_chat.id if update.effective_chat else None,
        update.effective_user.id if update.effective_user else None,
    )
    await update.message.reply_text(output_text)


def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is missing. Please set it in .env")

    db_path = os.getenv("SQLITE_PATH", "omnic.sqlite3")
    db_conn, _ = connect_sqlite(db_path)
    setup_echo_feature(db_conn)

    app = Application.builder().token(token).build()
    app.bot_data["db_conn"] = db_conn
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        db_conn.close()


if __name__ == "__main__":
    main()
