import asyncio
import hashlib
import logging
import os
import random
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, request
import psycopg
from psycopg.rows import dict_row
from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import Application, CommandHandler, ContextTypes


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "").strip()
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
WEBHOOK_BASE_URL = os.environ.get(
    "WEBHOOK_BASE_URL",
    os.environ.get("RENDER_EXTERNAL_URL", ""),
).strip().rstrip("/")

health_app = Flask(__name__)
telegram_application = None
telegram_loop = None
telegram_ready = threading.Event()
telegram_startup_error = None


@health_app.get("/")
@health_app.get("/health")
def health():
    return jsonify(status="ok", service="Книжковий Джин"), 200


def webhook_path() -> str:
    secret = hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest()[:32]
    return f"/telegram/{secret}"


@health_app.post("/telegram/<secret>")
def telegram_webhook(secret):
    expected = webhook_path().rsplit("/", 1)[-1]
    if secret != expected:
        return jsonify(status="not found"), 404

    if not telegram_ready.is_set():
        return jsonify(status="starting"), 503

    update = Update.de_json(request.get_json(force=True), telegram_application.bot)
    future = asyncio.run_coroutine_threadsafe(
        telegram_application.process_update(update),
        telegram_loop,
    )

    try:
        future.result(timeout=25)
    except Exception:
        logger.exception("Could not process Telegram webhook update")
        return jsonify(status="error"), 500

    return jsonify(status="ok"), 200


def get_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_database():
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS giveaways (
                id BIGSERIAL PRIMARY KEY,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS participants (
                giveaway_id BIGINT NOT NULL REFERENCES giveaways(id),
                user_id BIGINT NOT NULL,
                label TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (giveaway_id, user_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS winners (
                giveaway_id BIGINT NOT NULL REFERENCES giveaways(id),
                user_id BIGINT NOT NULL,
                label TEXT NOT NULL,
                selected_at TEXT NOT NULL,
                PRIMARY KEY (giveaway_id, user_id)
            )
            """
        )

        active = connection.execute(
            "SELECT id FROM giveaways WHERE status = 'active' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

        if active is None:
            connection.execute(
                "INSERT INTO giveaways (created_at, status) "
                "VALUES (%s, 'active')",
                (datetime.now(timezone.utc).isoformat(),),
            )


def get_active_giveaway_id() -> int:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT id FROM giveaways WHERE status = 'active' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

    if row is None:
        raise RuntimeError("No active giveaway")
    return int(row["id"])


def is_active_member(chat_member) -> bool:
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


async def check_membership(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> bool:
    member = await context.bot.get_chat_member(
        chat_id=CHANNEL_USERNAME,
        user_id=user_id,
    )
    return is_active_member(member)


async def start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.effective_user is None or update.effective_message is None:
        return

    user = update.effective_user

    try:
        subscribed = await check_membership(context, user.id)
    except Exception as exc:
        logger.exception(
            "Could not verify channel membership for user %s: %s",
            user.id,
            exc,
        )
        await update.effective_message.reply_text(
            "Не вдалося перевірити підписку. "
            "Спробуйте ще раз трохи пізніше."
        )
        return

    if not subscribed:
        await update.effective_message.reply_text(
            f"Щоб взяти участь у розіграші, підпишіться на канал "
            f"{CHANNEL_USERNAME} 📚\n\n"
            "Після підписки поверніться до Книжкового Джина "
            "і натисніть «Розпочати» ✨"
        )
        return

    giveaway_id = get_active_giveaway_id()
    label = participant_label(user)

    with get_connection() as connection:
        existing = connection.execute(
            """
            SELECT 1
            FROM participants
            WHERE giveaway_id = %s AND user_id = %s
            """,
            (giveaway_id, user.id),
        ).fetchone()

        if existing is not None:
            await update.effective_message.reply_text(
                "Ви вже берете участь 📚"
            )
            return

        connection.execute(
            """
            INSERT INTO participants
                (giveaway_id, user_id, label, joined_at)
            VALUES (%s, %s, %s, %s)
            """,
            (
                giveaway_id,
                user.id,
                label,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    await update.effective_message.reply_text(
        "Вітаємо! Ви берете участь у розіграші "
        "«Книжкового Джина» 📚✨"
    )


async def winners(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.effective_user is None or update.effective_message is None:
        return

    if update.effective_user.id != ADMIN_ID:
        await update.effective_message.reply_text(
            "Ця команда доступна лише адміністратору."
        )
        return

    if len(context.args) != 1:
        await update.effective_message.reply_text(
            "Використання: /winners 1, /winners 2 або /winners 3"
        )
        return

    try:
        count = int(context.args[0])
    except ValueError:
        count = 0

    if count not in {1, 2, 3}:
        await update.effective_message.reply_text(
            "Кількість переможців може бути 1, 2 або 3."
        )
        return

    giveaway_id = get_active_giveaway_id()

    with get_connection() as connection:
        participant_rows = connection.execute(
            """
            SELECT user_id, label
            FROM participants
            WHERE giveaway_id = %s
            """,
            (giveaway_id,),
        ).fetchall()

    eligible = []
    unsubscribed = []
    verification_errors = 0

    for row in participant_rows:
        user_id = int(row["user_id"])

        try:
            if await check_membership(context, user_id):
                eligible.append((user_id, row["label"]))
            else:
                unsubscribed.append(user_id)
        except Exception:
            verification_errors += 1
            logger.exception(
                "Could not re-check channel membership for user %s",
                user_id,
            )

    if count > len(eligible):
        details = (
            f"Недостатньо допущених учасників: потрібно {count}, "
            f"доступно {len(eligible)}."
        )

        if unsubscribed:
            details += (
                f" Не допущено через відсутність підписки: "
                f"{len(unsubscribed)}."
            )

        if verification_errors:
            details += (
                f" Не вдалося перевірити: {verification_errors}."
            )

        await update.effective_message.reply_text(details)
        return

    selected = random.SystemRandom().sample(eligible, count)

    with get_connection() as connection:
        connection.execute(
            "DELETE FROM winners WHERE giveaway_id = %s",
            (giveaway_id,),
        )

        for user_id, label in selected:
            connection.execute(
                """
                INSERT INTO winners
                    (giveaway_id, user_id, label, selected_at)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    giveaway_id,
                    user_id,
                    label,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    lines = [
        f"{index}. {label}"
        for index, (_, label) in enumerate(selected, start=1)
    ]

    result = "Переможці 🎉\n\n" + "\n".join(lines)

    if unsubscribed:
        result += (
            f"\n\nНе допущено через відсутність підписки: "
            f"{len(unsubscribed)}."
        )

    if verification_errors:
        result += (
            f"\nНе вдалося перевірити "
            f"(не брали участі у виборі): {verification_errors}."
        )

    await update.effective_message.reply_text(result)


async def new_giveaway(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.effective_user is None or update.effective_message is None:
        return

    if update.effective_user.id != ADMIN_ID:
        await update.effective_message.reply_text(
            "Ця команда доступна лише адміністратору."
        )
        return

    with get_connection() as connection:
        current = connection.execute(
            "SELECT id FROM giveaways WHERE status = 'active' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

        if current is not None:
            connection.execute(
                "DELETE FROM participants WHERE giveaway_id = %s",
                (int(current["id"]),),
            )

        connection.execute(
            "UPDATE giveaways SET status = 'closed' "
            "WHERE status = 'active'"
        )
        row = connection.execute(
            """
            INSERT INTO giveaways (created_at, status)
            VALUES (%s, 'active')
            RETURNING id
            """,
            (datetime.now(timezone.utc).isoformat(),),
        ).fetchone()
        new_id = int(row["id"])

    await update.effective_message.reply_text(
        f"Новий розіграш №{new_id} розпочато 📚✨\n"
        "Список учасників нового розіграшу порожній. "
        "Усі можуть брати участь знову."
    )


async def participants_count(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.effective_user is None or update.effective_message is None:
        return

    if update.effective_user.id != ADMIN_ID:
        await update.effective_message.reply_text(
            "Ця команда доступна лише адміністратору."
        )
        return

    giveaway_id = get_active_giveaway_id()

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS total
            FROM participants
            WHERE giveaway_id = %s
            """,
            (giveaway_id,),
        ).fetchone()

    await update.effective_message.reply_text(
        f"У розіграші №{giveaway_id} зареєстровано "
        f"{int(row['total'])} учасників 📚"
    )


def run_telegram_application() -> None:
    global telegram_loop, telegram_startup_error

    telegram_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(telegram_loop)

    async def start_application():
        await telegram_application.initialize()
        await telegram_application.start()
        await telegram_application.bot.set_webhook(
            url=f"{WEBHOOK_BASE_URL}{webhook_path()}",
            allowed_updates=Update.ALL_TYPES,
        )

    try:
        telegram_loop.run_until_complete(start_application())
        telegram_ready.set()
        logger.info("Telegram webhook is ready")
        telegram_loop.run_forever()
    except Exception as exc:
        telegram_startup_error = exc
        telegram_ready.set()
        logger.exception("Telegram application could not start")


def load_admin_id() -> int:
    try:
        return int(ADMIN_ID_RAW)
    except ValueError as exc:
        raise RuntimeError(
            "ADMIN_ID must be a numeric Telegram user ID"
        ) from exc


def validate_configuration() -> None:
    missing = [
        name
        for name, value in (
            ("BOT_TOKEN", BOT_TOKEN),
            ("CHANNEL_USERNAME", CHANNEL_USERNAME),
            ("ADMIN_ID", ADMIN_ID_RAW),
            ("DATABASE_URL", DATABASE_URL),
            ("WEBHOOK_BASE_URL or RENDER_EXTERNAL_URL", WEBHOOK_BASE_URL),
        )
        if not value
    ]

    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )


if __name__ == "__main__":
    validate_configuration()
    ADMIN_ID = load_admin_id()
    init_database()

    telegram_application = Application.builder().token(BOT_TOKEN).build()

    telegram_application.add_handler(CommandHandler("start", start))
    telegram_application.add_handler(CommandHandler("winners", winners))
    telegram_application.add_handler(CommandHandler("new", new_giveaway))
    telegram_application.add_handler(
        CommandHandler("participants", participants_count)
    )

    threading.Thread(
        target=run_telegram_application,
        daemon=True,
    ).start()
    telegram_ready.wait(timeout=30)

    if telegram_startup_error is not None:
        raise RuntimeError("Telegram application failed to start") from telegram_startup_error

    if not telegram_ready.is_set():
        raise RuntimeError("Telegram application startup timed out")

    port = int(os.environ.get("PORT", "10000"))
    health_app.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False,
    )
