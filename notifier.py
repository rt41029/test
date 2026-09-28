import os
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
