"""Обёртки Telegram Bot API. Читают TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
из окружения при каждом вызове (удобно для multiprocessing spawn)."""
import os
import time
import json
import logging
import requests

logger = logging.getLogger(__name__)


def _creds():
    return os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")


def _markup(buttons):
    if not buttons:
        return None
    return json.dumps({
        "inline_keyboard": [
            [{"text": t, "callback_data": d} for t, d in row] for row in buttons
        ]
    })


def send_telegram(message: str, buttons=None):
    token, chat_id = _creds()
    if not token or not chat_id:
        logger.warning("Telegram не настроен — пропускаю")
        return
    data = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    mk = _markup(buttons)
    if mk:
        data["reply_markup"] = mk
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data, timeout=15,
        )
        if r.status_code != 200:
            logger.warning(f"Telegram вернул {r.status_code}")
    except Exception as e:
        logger.warning(f"sendMessage: {type(e).__name__}")


def send_photo(png_bytes: bytes, caption: str = "", buttons=None):
    token, chat_id = _creds()
    if not token or not chat_id:
        return
    data = {"chat_id": chat_id, "caption": caption[:1000], "parse_mode": "HTML"}
    mk = _markup(buttons)
    if mk:
        data["reply_markup"] = mk
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data=data,
            files={"photo": ("screen.png", png_bytes)},
            timeout=30,
        )
    except Exception as e:
        logger.warning(f"sendPhoto: {type(e).__name__}")


def answer_callback(callback_id: str, text: str = ""):
    token, _ = _creds()
    if not token:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            data={"callback_query_id": callback_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"answerCallbackQuery: {type(e).__name__}")


def get_update_offset():
    """offset после последнего известного апдейта (чтобы не получать старые)."""
    token, _ = _creds()
    if not token:
        return None
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": -1, "timeout": 0},
            timeout=15,
        ).json()
        res = r.get("result", [])
        return res[-1]["update_id"] + 1 if res else 0
    except Exception as e:
        logger.warning(f"getUpdates: {type(e).__name__}")
        return None


def get_updates(offset, timeout=8):
    """Long-poll. Возвращает (события, новый_offset).
    События только из вашего чата:
      ("cb", callback_id, data)  — нажатие кнопки
      ("text", None, текст)      — текстовое сообщение (ответ на капчу)"""
    token, chat_id = _creds()
    if not token or not chat_id:
        time.sleep(timeout)
        return [], offset
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
            timeout=timeout + 10,
        ).json()
    except Exception as e:
        logger.warning(f"get_updates: {type(e).__name__}")
        time.sleep(3)
        return [], offset

    events = []
    for upd in r.get("result", []):
        offset = upd["update_id"] + 1
        cq = upd.get("callback_query")
        if cq:
            if str(cq.get("message", {}).get("chat", {}).get("id")) == str(chat_id):
                events.append(("cb", cq["id"], cq.get("data", "")))
            continue
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) == str(chat_id) and msg.get("text"):
            events.append(("text", None, msg["text"].strip()))
    return events, offset
