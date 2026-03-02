import os
import logging
import json
import asyncio
import re
from urllib import request, error

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    Message,
    Update,
)
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from db import (
    clear_summary_start,
    connect_sqlite,
    count_messages_since,
    fetch_messages_since,
    fetch_recent_messages,
    get_latest_message_id,
    get_latest_user_name,
    get_summary_start,
    list_group_chat_ids,
    list_summary_positions_by_chat,
    record_message_event,
    set_summary_start,
    setup_echo_feature,
    setup_summary_feature,
)

logger = logging.getLogger(__name__)
MAX_SUMMARY_MESSAGES = 1000


def _setup_logging_from_env() -> None:
    raw_level = os.getenv("LOG_LEVEL", "DEBUG")
    level = getattr(logging, raw_level.upper(), None)
    if not isinstance(level, int):
        level = logging.DEBUG
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        )
        logger.warning("Invalid LOG_LEVEL=%r, fallback to DEBUG", raw_level)
        return

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def _normalize_mixed_zh_en_spacing(text: str) -> str:
    normalized = re.sub(r"([\u4e00-\u9fff])([A-Za-z0-9])", r"\1 \2", text)
    normalized = re.sub(r"([A-Za-z0-9])([\u4e00-\u9fff])", r"\1 \2", normalized)
    return normalized


async def _reply_text(update: Update, text: str) -> None:
    if not update.message:
        return
    await update.message.reply_text(_normalize_mixed_zh_en_spacing(text))


def _build_summary_prompt(messages: list[tuple[int, int | None, str | None, str]]) -> str:
    known_names: dict[int, str] = {}
    for _, user_id, user_name, _ in messages:
        if user_id is not None and user_name:
            known_names[user_id] = user_name

    lines = []
    for message_id, user_id, user_name, text in messages:
        display_name = user_name
        if not display_name and user_id is not None:
            display_name = known_names.get(user_id)
        if not display_name:
            display_name = "unknown_user"
        lines.append(f"[{message_id}] {display_name}: {text}")
    return "\n".join(lines)


def _build_gpt_prompt(question: str, reply_context: str | None, quote_context: str | None) -> str:
    sections = [f"Question:\n{question.strip()}"]
    if reply_context:
        sections.append(f"Reply context:\n{reply_context}")
    if quote_context:
        sections.append(f"Quote context:\n{quote_context}")
    return "\n\n".join(sections)


def _extract_message_text(message: Message | None) -> str | None:
    if message is None:
        return None
    text = message.text or message.caption
    if text is None:
        return None
    normalized = str(text).strip()
    if not normalized:
        return None
    return normalized


def _extract_reply_context(message: Message) -> str | None:
    reply_message = message.reply_to_message
    reply_text = _extract_message_text(reply_message)
    if reply_text is None:
        return None

    display_name = "unknown_user"
    if reply_message is not None and reply_message.from_user is not None:
        display_name = (
            reply_message.from_user.username
            or reply_message.from_user.full_name
            or "unknown_user"
        )
    return f"{display_name}: {reply_text}"


def _extract_quote_context(message: Message) -> str | None:
    quote = getattr(message, "quote", None)
    if quote is None:
        return None
    quote_text = getattr(quote, "text", None)
    if quote_text is None:
        return None
    normalized = str(quote_text).strip()
    if not normalized:
        return None
    return normalized


def _call_openai_chat(
    prompt: str,
    bot_data: dict,
    extra_system_prompt: str | None = None,
    mode: str = "summary",
) -> str:
    api_key = bot_data.get("openai_api_key")
    api_base = bot_data.get("openai_base_url", "https://api.openai.com/v1")
    model = bot_data.get("openai_chat_model", "gpt-4.1-mini")

    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing. Please set it in .env")

    url = f"{api_base.rstrip('/')}/chat/completions"
    if mode == "summary":
        system_content = (
            "You are a chat summarizer for a Telegram group. "
            "Return a short, natural Chinese summary in plain paragraphs. "
            "Avoid rigid sections or bullet templates."
        )
    elif mode == "assistant":
        system_content = (
            "You are a helpful assistant for a Telegram group. "
            "Use provided context when available and answer accurately in concise Chinese."
        )
    else:
        raise RuntimeError(f"Unsupported OpenAI chat mode: {mode}")

    messages_payload: list[dict[str, str]] = [{"role": "system", "content": system_content}]
    if extra_system_prompt:
        messages_payload.append(
            {
                "role": "system",
                "content": f"Additional user preference for this summary: {extra_system_prompt}",
            }
        )
    messages_payload.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages_payload,
        "temperature": 0.2,
    }

    req = request.Request(
        url,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        data=json.dumps(payload).encode("utf-8"),
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenAI API HTTP {exc.code}: {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"OpenAI API network error: {exc}") from exc

    choices = body.get("choices")
    if not choices:
        raise RuntimeError("OpenAI API returned no choices")
    message = choices[0].get("message", {})
    content = message.get("content", "")
    if not content:
        raise RuntimeError("OpenAI API returned empty content")
    return str(content).strip()


def _validate_openai_config(bot_data: dict) -> None:
    try:
        reply = _call_openai_chat("Reply with exactly OK.", bot_data)
    except RuntimeError as exc:
        message = str(exc)
        lower_message = message.lower()
        if "quota" in lower_message or "insufficient_quota" in lower_message:
            raise RuntimeError(f"OpenAI quota validation failed: {message}") from exc
        raise RuntimeError(f"OpenAI config validation failed: {message}") from exc
    if not reply:
        raise RuntimeError("OpenAI config validation failed: empty response")


def _send_startup_notice(token: str, chat_ids: list[int], text: str) -> None:
    if not chat_ids:
        logger.info("No known group chats in SQLite; skip startup notice")
        return

    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in chat_ids:
        payload = {"chat_id": chat_id, "text": _normalize_mixed_zh_en_spacing(text)}
        req = request.Request(
            api_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload).encode("utf-8"),
        )
        try:
            with request.urlopen(req, timeout=15):
                pass
        except Exception as exc:
            logger.warning("Failed to send startup notice to chat_id=%s: %s", chat_id, exc)


async def _setup_command_menu(application: Application) -> None:
    commands = [
        BotCommand("start", "Check bot status"),
        BotCommand("summary", "Usage: /summary <count> [prompt]|start|end [prompt]|usage"),
        BotCommand("gpt", "Usage: /gpt <question>"),
    ]
    await application.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    await application.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    await application.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
    await application.bot.set_my_commands(commands, scope=BotCommandScopeAllChatAdministrators())


async def _summarize_messages(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    messages: list[tuple[int, int | None, str | None, str]],
    prefix: str | None = None,
    extra_system_prompt: str | None = None,
) -> None:
    if not update.message:
        return
    if not messages:
        await _reply_text(update, "没有可总结的消息。")
        return

    prompt = _build_summary_prompt(messages)
    try:
        summary = await asyncio.to_thread(
            _call_openai_chat, prompt, context.application.bot_data, extra_system_prompt
        )
    except Exception as exc:
        logger.exception("Failed to summarize messages: %s", exc)
        await _reply_text(update, f"总结失败：{exc}")
        return

    if prefix:
        await _reply_text(update, f"{prefix}\n\n{summary}")
        return
    await _reply_text(update, summary)


async def _handle_summary_end(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    source: str,
    extra_system_prompt: str | None = None,
) -> None:
    if not update.message:
        return

    db_conn = context.application.bot_data.get("db_conn")
    if db_conn is None:
        await _reply_text(update, "数据库未初始化。")
        return

    start_message_id = get_summary_start(db_conn, chat_id, user_id)
    if start_message_id is None:
        await _reply_text(update, "你还没有 start。先执行 /summary start。")
        return

    await _finalize_summary_for_user(
        update=update,
        context=context,
        chat_id=chat_id,
        target_user_id=user_id,
        start_message_id=start_message_id,
        source=source,
        extra_system_prompt=extra_system_prompt,
    )


async def _finalize_summary_for_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    target_user_id: int,
    start_message_id: int,
    source: str,
    extra_system_prompt: str | None = None,
) -> None:
    if not update.message:
        return

    db_conn = context.application.bot_data.get("db_conn")
    if db_conn is None:
        await _reply_text(update, "数据库未初始化。")
        return

    total_count = count_messages_since(db_conn, chat_id, start_message_id)
    if total_count == 0:
        clear_summary_start(db_conn, chat_id, target_user_id)
        await _reply_text(update, "start 之后没有新消息，已清理该 summary 位置。")
        return

    over_limit = total_count > MAX_SUMMARY_MESSAGES
    message_limit = MAX_SUMMARY_MESSAGES if over_limit else None
    messages = fetch_messages_since(
        db_conn,
        chat_id=chat_id,
        start_message_id=start_message_id,
        limit=message_limit,
    )
    clear_summary_start(db_conn, chat_id, target_user_id)
    target_user_name = get_latest_user_name(db_conn, chat_id, target_user_id) or "unknown_user"

    prefix = None
    if over_limit:
        prefix = (
            f"{target_user_name} 的 start 到现在共有 {total_count} 条消息，超过 {MAX_SUMMARY_MESSAGES} 条上下文限制。"
            f"已按最早的 {MAX_SUMMARY_MESSAGES} 条执行 end 总结并清理位置。"
        )
    elif source == "auto":
        prefix = f"{target_user_name} 已达到上下文上限，自动执行 end 总结并清理位置。"

    await _summarize_messages(
        update, context, messages, prefix=prefix, extra_system_prompt=extra_system_prompt
    )


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat or not update.effective_user:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    db_conn = context.application.bot_data.get("db_conn")
    if db_conn is None:
        await _reply_text(update, "数据库未初始化。")
        return

    if not context.args:
        await _reply_text(update, "用法：/summary <count> [prompt]|start|end [prompt]|usage")
        return

    active_start_id = get_summary_start(db_conn, chat_id, user_id)
    if active_start_id is not None:
        pending_count = count_messages_since(db_conn, chat_id, active_start_id)
        if pending_count > MAX_SUMMARY_MESSAGES:
            await _handle_summary_end(update, context, chat_id, user_id, source="auto")
            return

    arg0 = context.args[0].strip().lower()
    if arg0 == "start":
        current_latest_id = get_latest_message_id(db_conn, chat_id)
        inserted = set_summary_start(db_conn, chat_id, user_id, current_latest_id)
        if not inserted:
            await _reply_text(update, "你已经 start 过了。请先 /summary end。")
            return
        await _reply_text(update, "已记录 summary 起点。后续执行 /summary end 将总结从该位置到当前的消息。")
        return

    if arg0 == "end":
        extra_system_prompt = " ".join(context.args[1:]).strip() or None
        await _handle_summary_end(
            update,
            context,
            chat_id,
            user_id,
            source="manual",
            extra_system_prompt=extra_system_prompt,
        )
        return

    if arg0 == "usage":
        start_message_id = get_summary_start(db_conn, chat_id, user_id)
        if start_message_id is None:
            await _reply_text(
                update,
                f"你当前没有 active start。\n已记录: 0 条\n剩余可用: {MAX_SUMMARY_MESSAGES} 条\n先执行 /summary start 开始计数。"
            )
            return
        used_count = count_messages_since(db_conn, chat_id, start_message_id)
        remain_count = max(0, MAX_SUMMARY_MESSAGES - used_count)
        await _reply_text(
            update,
            f"当前 summary context 状态：\n已记录: {used_count} 条\n剩余可用: {remain_count} 条"
        )
        return

    try:
        count = int(arg0)
    except ValueError:
        await _reply_text(update, "参数无效。用法：/summary <count> [prompt]|start|end [prompt]|usage")
        return

    if count <= 0:
        await _reply_text(update, "count 必须是正整数。")
        return

    over_limit = count > MAX_SUMMARY_MESSAGES
    if over_limit:
        count = MAX_SUMMARY_MESSAGES

    messages = fetch_recent_messages(db_conn, chat_id, count)
    if not messages:
        await _reply_text(update, "没有可总结的历史消息。")
        return

    prefix = None
    if over_limit:
        prefix = f"请求条数超过 {MAX_SUMMARY_MESSAGES}，已按 {MAX_SUMMARY_MESSAGES} 条执行总结。"
    extra_system_prompt = " ".join(context.args[1:]).strip() or None
    await _summarize_messages(
        update,
        context,
        messages,
        prefix=prefix,
        extra_system_prompt=extra_system_prompt,
    )


async def gpt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    question = " ".join(context.args).strip()
    reply_context = _extract_reply_context(update.message)
    quote_context = _extract_quote_context(update.message)

    if not question:
        if reply_context is None and quote_context is None:
            await _reply_text(update, "用法：/gpt <问题>（可结合 reply/quote）")
            return
        question = "请基于上下文回答这个问题。"

    prompt = _build_gpt_prompt(question, reply_context, quote_context)
    try:
        answer = await asyncio.to_thread(
            _call_openai_chat,
            prompt,
            context.application.bot_data,
            None,
            "assistant",
        )
    except Exception as exc:
        logger.exception("Failed to answer gpt request: %s", exc)
        await _reply_text(update, f"GPT 回答失败：{exc}")
        return

    await _reply_text(update, answer)


async def _maybe_auto_finalize_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    db_conn = context.application.bot_data.get("db_conn")
    if db_conn is None:
        return

    chat_id = update.effective_chat.id
    positions = list_summary_positions_by_chat(db_conn, chat_id)
    for target_user_id, start_message_id in positions:
        total_count = count_messages_since(db_conn, chat_id, start_message_id)
        if total_count <= MAX_SUMMARY_MESSAGES:
            continue
        await _finalize_summary_for_user(
            update=update,
            context=context,
            chat_id=chat_id,
            target_user_id=target_user_id,
            start_message_id=start_message_id,
            source="auto",
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    output_text = "Bot is alive. Summary service is ready."
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
    await _reply_text(update, output_text)


async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or update.message.text is None:
        return

    input_text = update.message.text

    db_conn = context.application.bot_data.get("db_conn")
    if db_conn is not None:
        user_name = None
        if update.effective_user:
            user_name = (
                update.effective_user.username
                or update.effective_user.full_name
                or "unknown_user"
            )
        record_message_event(
            db_conn,
            update.effective_chat.id if update.effective_chat else None,
            update.effective_user.id if update.effective_user else None,
            user_name,
            input_text,
        )

    logger.debug(
        "input: %r | chat_id=%s user_id=%s",
        input_text,
        update.effective_chat.id if update.effective_chat else None,
        update.effective_user.id if update.effective_user else None,
    )
    await _maybe_auto_finalize_summary(update, context)


def main() -> None:
    load_dotenv()
    _setup_logging_from_env()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is missing. Please set it in .env")

    db_path = os.getenv("SQLITE_PATH", "omnic.sqlite3")
    db_conn, _ = connect_sqlite(db_path)
    setup_echo_feature(db_conn)
    setup_summary_feature(db_conn)

    app = Application.builder().token(token).post_init(_setup_command_menu).build()
    app.bot_data["db_conn"] = db_conn
    app.bot_data["openai_api_key"] = os.getenv("OPENAI_API_KEY")
    app.bot_data["openai_base_url"] = os.getenv(
        "OPENAI_BASE_URL",
        os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1"),
    )
    app.bot_data["openai_chat_model"] = os.getenv("OPENAI_CHAT_MODEL", "gpt-4.1-mini")

    group_chat_ids = list_group_chat_ids(db_conn)
    try:
        _validate_openai_config(app.bot_data)
        _send_startup_notice(token, group_chat_ids, "OpenAI config check passed. Summary service is ready.")
    except Exception as exc:
        _send_startup_notice(token, group_chat_ids, f"OpenAI config check failed: {exc}")
        raise

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("gpt", gpt))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        db_conn.close()


if __name__ == "__main__":
    main()
