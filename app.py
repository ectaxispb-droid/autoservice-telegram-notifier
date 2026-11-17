import hmac
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from html import escape

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

BOT_TOKEN = os.environ[322894971:AAF_vX7sLANWOEPgdhR_kkcDy_xWKF1ltNg]
API_SECRET = os.environ[autoservicesecret]
WEBHOOK_SECRET = os.environ[autoservicesecret2]
DB_PATH = os.getenv("DATABASE_PATH", "./data/notifier.sqlite3")

# JSON: {"id-механика": "длинный-случайный-код-привязки"}
LINK_CODES = json.loads(os.getenv("MECHANIC_LINK_CODES", "{}"))

app = FastAPI(title="Autoservice Telegram Notifier")


@contextmanager
def db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mechanics (
                mechanic_id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                linked_at INTEGER NOT NULL
            )
        """)
        conn.commit()
        yield conn
    finally:
        conn.close()


def telegram(method: str, payload: dict):
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            result = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Telegram API недоступен") from exc

    if not result.get("ok"):
        raise RuntimeError("Telegram отклонил запрос")


class Appointment(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    mechanic_id: str = Field(min_length=1, max_length=128)
    event_type: str = Field(pattern="^(new|updated|cancelled)$")
    starts_at: str = Field(min_length=1, max_length=80)
    vehicle: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=500)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, WEBHOOK_SECRET
    ):
        raise HTTPException(403, "forbidden")

    update = await request.json()
    message = update.get("message") or {}
    text = message.get("text", "")
    chat_id = message.get("chat", {}).get("id")

    if not chat_id or not text.startswith("/start"):
        return {"ok": True}

    parts = text.split(maxsplit=1)
    code = parts[1].strip() if len(parts) == 2 else ""

    mechanic_id = next(
        (
            mid for mid, candidate in LINK_CODES.items()
            if hmac.compare_digest(str(candidate), code)
        ),
        None,
    )

    if mechanic_id:
        with db() as conn:
            conn.execute(
                """
                INSERT INTO mechanics(mechanic_id, chat_id, linked_at)
                VALUES (?, ?, ?)
                ON CONFLICT(mechanic_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    linked_at=excluded.linked_at
                """,
                (mechanic_id, str(chat_id), int(time.time())),
            )
            conn.commit()
        reply = "Уведомления Autoservice подключены."
    else:
        reply = "Для подключения откройте персональную ссылку от администратора Autoservice."

    try:
        telegram("sendMessage", {"chat_id": chat_id, "text": reply})
    except RuntimeError:
        pass

    return {"ok": True}


@app.post("/api/appointments")
def notify(
    item: Appointment,
    authorization: str | None = Header(default=None),
):
    expected = "Bearer " + API_SECRET
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "unauthorized")

    with db() as conn:
        row = conn.execute(
            "SELECT chat_id FROM mechanics WHERE mechanic_id = ?",
            (item.mechanic_id,),
        ).fetchone()

    if not row:
        raise HTTPException(404, "mechanic_not_linked")

    labels = {
        "new": "Новая запись",
        "updated": "Запись изменена",
        "cancelled": "Запись отменена",
    }
    lines = [
        f"<b>{labels[item.event_type]}</b>",
        f"Время: {escape(item.starts_at)}",
    ]
    if item.vehicle:
        lines.append(f"Автомобиль: {escape(item.vehicle)}")
    if item.description:
        lines.append(f"Детали: {escape(item.description)}")

    try:
        telegram(
            "sendMessage",
            {
                "chat_id": row["chat_id"],
                "text": "\n".join(lines),
                "parse_mode": "HTML",
            },
        )
    except RuntimeError:
        raise HTTPException(502, "telegram_delivery_failed")

    return {"ok": True}
