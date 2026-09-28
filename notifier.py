import os
import time
import logging
import requests

logger = logging.getLogger(__name__)


def _creds():
    # читаем переменные при вызове, а не при импорте
    return os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")


def send_telegram(message: str):
    token, chat_id = _creds()
    if not token or not chat_id:
        logger.warning("Telegram не настроен — пропускаю уведомление")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning(f"Telegram вернул {r.status_code}")
    except Exception as e:
        logger.warning(f"Не удалось отправить в Telegram: {type(e).__name__}")


def send_photo(png_bytes: bytes, caption: str = ""):
    """Скриншот в Telegram (для отладки, когда бот не смог зайти)."""
    token, chat_id = _creds()
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption[:1000], "parse_mode": "HTML"},
            files={"photo": ("screen.png", png_bytes)},
            timeout=30,
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить фото: {type(e).__name__}")


def get_update_offset():
    """Смещение, после которого идут только НОВЫЕ сообщения (вызывать до отправки капчи)."""
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


def wait_for_reply(offset, timeout=180):
    """Ждёт текстовое сообщение из вашего чата. Возвращает текст или None."""
    token, chat_id = _creds()
    if not token or not chat_id or offset is None:
        return None
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": offset, "timeout": 10},
                timeout=25,
            ).json()
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                if str(msg.get("chat", {}).get("id")) == str(chat_id) and msg.get("text"):
                    return msg["text"].strip()
        except Exception as e:
            logger.warning(f"wait_for_reply: {type(e).__name__}")
            time.sleep(3)
    return None
