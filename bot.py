import logging
import os
import random
import threading

from flask import Flask, jsonify
from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import Application, CommandHandler, ContextTypes


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "").strip()
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", "").strip()

# MVP storage only: Render restarts/redeploys clear this participant list.
participants: dict[int, str] = {}

health_app = Flask(__name__)


@health_app.get("/")
@health_app.get("/health")
def health():
    return jsonify(status="ok", service="Книжковий Джин"), 200


def is_active_member(chat_member) -> bool:
    """Return True for active channel members, including restricted members."""
    if chat_member.status in {
        ChatMemberStatus.OWNER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
    }:
        return True
    return (
        chat_member.status == ChatMemberStatus.RESTRICTED
        and bool(chat_member.is_member)
    )


def participant_label(user) -> str:
    if user.username:
        return f"@{user.username}"
    name = user.full_name.strip() or "Учасник"
    return f"{name} (ID: {user.id})"


async def check_membership(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    member = await context.bot.get_chat_member(
        chat_id=CHANNEL_USERNAME,
        user_id=user_id,
    )
    return is_active_member(member)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.effective_message is None:
        return

    user = update.effective_user---===-------==
    try:
        subscribed = await check_membership(context, user.id)
    except Exception as exc:
        ogger.exception("Could not verify channel membership for user %s: %s", user.id, exc)
        await update.effective_message.reply_text(
            "Не вдалося перевірити підписку. Спробуйте ще раз трохи пізніше."
        )
        return

    if not subscribed:
        await update.effective_message.reply_text(
            f"Щоб взяти участь, підпишіться на канал {CHANNEL_USERNAME}, "
            "а потім знову натисніть /start."
        )
        return

    if user.id in participants:
        await update.effective_message.reply_text("Ви вже берете участь")
        return

    participants[user.id] = participant_label(user)
    await update.effective_message.reply_text(
        "Вітаємо! Ви берете участь у розіграші «Книжкового Джина» 📚"
    )


async def winners(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.effective_message is None:
        return

    if update.effective_user.id != ADMIN_ID:
        await update.effective_message.reply_text("Ця команда доступна лише адміністратору.")
        return

    if len(context.args) != 1:
        await update.effective_message.reply_text("Використання: /winners N")
        return

    try:
        count = int(context.args[0])
        if count < 1:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("N має бути цілим додатним числом.")
        return

    eligible_ids: list[int] = []
    unsubscribed_ids: list[int] = []
    verification_errors = 0

    for user_id in list(participants):
        try:
            if await check_membership(context, user_id):
                eligible_ids.append(user_id)
            else:
                unsubscribed_ids.append(user_id)
        except Exception:
            verification_errors += 1
            logger.exception("Could not re-check channel membership for user %s", user_id)

    for user_id in unsubscribed_ids:
        participants.pop(user_id, None)

    if count > len(eligible_ids):
        details = (
            f"Недостатньо допущених учасників: потрібно {count}, "
            f"доступно {len(eligible_ids)}."
        )
        if verification_errors:
            details += f" Не вдалося перевірити: {verification_errors}."
        await update.effective_message.reply_text(details)
        return

    selected_ids = random.SystemRandom().sample(eligible_ids, count)
    lines = [
        f"{index}. {participants[user_id]}"
        for index, user_id in enumerate(selected_ids, start=1)
    ]
    result = "Переможці 🎉\n\n" + "\n".join(lines)
    if unsubscribed_ids:
        result += f"\n\nВідсіяно через відсутність підписки: {len(unsubscribed_ids)}."
    if verification_errors:
        result += f"\nНе вдалося перевірити (не брали участі у виборі): {verification_errors}."
    await update.effective_message.reply_text(result)


def run_health_server() -> None:
    port = int(os.environ.get("PORT", "10000"))
    health_app.run(host="0.0.0.0", port=port, use_reloader=False)


def load_admin_id() -> int:
    try:
        return int(ADMIN_ID_RAW)
    except ValueError as exc:
        raise RuntimeError("ADMIN_ID must be a numeric Telegram user ID") from exc


def validate_configuration() -> None:
    missing = [
        name
        for name, value in (
            ("BOT_TOKEN", BOT_TOKEN),
            ("CHANNEL_USERNAME", CHANNEL_USERNAME),
            ("ADMIN_ID", ADMIN_ID_RAW),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


if __name__ == "__main__":
    validate_configuration()
    ADMIN_ID = load_admin_id()

    threading.Thread(target=run_health_server, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("winners", winners))
    application.run_polling()
