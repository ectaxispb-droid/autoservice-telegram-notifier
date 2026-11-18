import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from html import escape
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return value


BOT_TOKEN = required_env("TELEGRAM_BOT_TOKEN")
API_SECRET = required_env("NOTIFIER_API_SECRET")
WEBHOOK_SECRET = required_env("TELEGRAM_WEBHOOK_SECRET")

BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "expressautomaster24_bot").lstrip("@")
DB_PATH = os.getenv("DATABASE_PATH", "/app/data/notifier.sqlite3")

app = FastAPI(title="Autoservice Telegram Notifier")


@contextmanager
def db():
    directory = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS link_codes (
                code_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                linked_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, chat_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_tenant "
            "ON subscriptions(tenant_id)"
        )
        conn.commit()
        yield conn
    finally:
        conn.close()


def telegram(method: str, payload: dict) -> None:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("Telegram API недоступен") from exc

    if not result.get("ok"):
        raise RuntimeError("Telegram отклонил запрос")


def verify_authorization(authorization: Optional[str]) -> None:
    expected = "Bearer " + API_SECRET
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


class LinkRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)


class EventRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    event_type: str = Field(min_length=1, max_length=64)
    event_id: str = Field(default="", max_length=128)
    starts_at: str = Field(default="", max_length=80)
    vehicle: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=500)
    appointment_url: str = Field(default="", max_length=500)
    client_name: str = Field(default="", max_length=200)
    client_phone: str = Field(default="", max_length=40)
    amount: Optional[float] = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/link")
def create_link(
    item: LinkRequest,
    authorization: Optional[str] = Header(default=None),
):
    verify_authorization(authorization)

    code = secrets.token_urlsafe(24)
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    now = int(time.time())
    expires_at = now + 24 * 60 * 60

    with db() as conn:
        conn.execute(
            "DELETE FROM link_codes WHERE expires_at < ?",
            (now,),
        )
        conn.execute(
            """
            INSERT INTO link_codes(code_hash, user_id, tenant_id, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (code_hash, item.user_id, item.tenant_id, now, expires_at),
        )
        conn.commit()

    link = f"https://t.me/{BOT_USERNAME}?start={quote(code, safe='')}"
    return {"url": link, "link": link, "expires_at": expires_at}


@app.get("/api/link/{user_id}")
def check_link(
    user_id: str,
    authorization: Optional[str] = Header(default=None),
):
    verify_authorization(authorization)

    with db() as conn:
        rows = conn.execute(
            """
            SELECT tenant_id, chat_id, linked_at
            FROM subscriptions
            WHERE user_id = ?
            ORDER BY linked_at DESC
            """,
            (user_id,),
        ).fetchall()

    return {
        "linked": bool(rows),
        "subscriptions": [
            {
                "tenant_id": row["tenant_id"],
                "chat_id": row["chat_id"],
                "linked_at": row["linked_at"],
            }
            for row in rows
        ],
    }


@app.delete("/api/link/{user_id}")
def delete_link(
    user_id: str,
    authorization: Optional[str] = Header(default=None),
):
    verify_authorization(authorization)

    with db() as conn:
        cursor = conn.execute(
            "DELETE FROM subscriptions WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()

    return {"ok": True, "deleted": cursor.rowcount}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    if (
        not x_telegram_bot_api_secret_token
        or not hmac.compare_digest(
            x_telegram_bot_api_secret_token,
            WEBHOOK_SECRET,
        )
    ):
        raise HTTPException(status_code=403, detail="forbidden")

    try:
        update = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_json")

    message = update.get("message") or {}
    text = message.get("text", "")
    chat_id = (message.get("chat") or {}).get("id")

    if not chat_id or not isinstance(text, str):
        return {"ok": True}

    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0] if parts else ""
    if command != "/start":
        return {"ok": True}

    code = parts[1].strip() if len(parts) == 2 else ""
    reply = "Ссылка подключения недействительна или устарела."

    if code:
        code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        now = int(time.time())

        with db() as conn:
            row = conn.execute(
                """
                SELECT user_id, tenant_id
                FROM link_codes
                WHERE code_hash = ? AND expires_at >= ?
                """,
                (code_hash, now),
            ).fetchone()

            if row:
                conn.execute(
                    """
                    INSERT INTO subscriptions(user_id, tenant_id, chat_id, linked_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id, chat_id) DO UPDATE SET
                        tenant_id = excluded.tenant_id,
                        linked_at = excluded.linked_at
                    """,
                    (row["user_id"], row["tenant_id"], str(chat_id), now),
                )
                conn.execute(
                    "DELETE FROM link_codes WHERE code_hash = ?",
                    (code_hash,),
                )
                conn.commit()
                reply = "🔔 Уведомления sto.autoimba.ru подключены."

    try:
        telegram("sendMessage", {"chat_id": chat_id, "text": reply})
    except RuntimeError:
        # Не провоцируем повторную доставку webhook от Telegram.
        pass

    return {"ok": True}


@app.post("/api/events")
def notify_event(
    item: EventRequest,
    authorization: Optional[str] = Header(default=None),
):
    verify_authorization(authorization)

    labels = {
    "new": "🆕 Новая запись",
    "updated": "✏️ Запись изменена",
    "cancelled": "❌ Запись отменена",
    "completed_paid": "✅ Заказ выполнен",
}
label = labels.get(item.event_type, f"🔔 Событие: {item.event_type}")

lines = [f"<b>{escape(label)}</b>"]

if item.client_name:
    lines.append(f"Клиент: {escape(item.client_name)}")
if item.client_phone:
    lines.append(f"Телефон: {escape(item.client_phone)}")
if item.starts_at:
    lines.append(f"Время: {escape(item.starts_at)}")
if item.vehicle:
    lines.append(f"Автомобиль: {escape(item.vehicle)}")
if item.description:
    lines.append(f"Детали: {escape(item.description)}")
if item.amount is not None:
    lines.append(f"Сумма: {item.amount:,.2f} ₽".replace(",", " "))

if item.appointment_url:
    safe_url = escape(item.appointment_url, quote=True)
    lines.append(f'<a href="{safe_url}">Открыть в sto.autoimba.ru</a>')

message = "\n".join(lines)
    sent = 0
    failed = 0

    for row in rows:
        try:
            telegram(
                "sendMessage",
                {
                    "chat_id": row["chat_id"],
                    "text": message,
                    "parse_mode": "HTML",
                },
            )
            sent += 1
        except RuntimeError:
            failed += 1

    if failed and not sent:
        raise HTTPException(status_code=502, detail="telegram_delivery_failed")

    return {"ok": True, "sent": sent, "failed": failed}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
