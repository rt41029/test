"""
Бот для GitHub Actions.
Запускается по cron, смотрит расписание (Киев), заходит на пары, которые
начинаются в ближайшие 20 минут или начались не более 25 минут назад,
и сидит до конца пары. Вход в Google — по cookies (если есть) или по почте и паролю.

Переменные окружения (Secrets):
  GOOGLE_EMAIL        почта Google-аккаунта
  GOOGLE_PASSWORD     пароль Google-аккаунта
  GOOGLE_COOKIES      (необязательно) JSON со списком cookies Google — пробуется первым
  SCHEDULE_JSON       содержимое schedule.json (если нет локального файла)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  TEST_INDEX          (необязательно) номер пары в списке — зайти на неё прямо
                      сейчас на 3 минуты, для проверки
  TEST_URL            (необязательно) ссылка на Meet — зайти на 5 минут
"""
import json
import os
import sys
import time
import logging
import multiprocessing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from notifier import send_telegram, send_photo, get_update_offset, wait_for_reply

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("bot")

try:
    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:
    KYIV = ZoneInfo("Europe/Kiev")

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
EARLY = timedelta(minutes=20)   # за сколько до начала можно заходить
LATE = timedelta(minutes=25)    # насколько можно опоздать (cron в Actions бывает с задержкой)
JOIN_LEAD = timedelta(minutes=7)  # за сколько до начала вставать в очередь на вход

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ======================= РАСПИСАНИЕ =======================
def load_schedule():
    raw = os.getenv("SCHEDULE_JSON")
    if raw:
        return json.loads(raw)
    with open("schedule.json", "r", encoding="utf-8") as f:
        return json.load(f)


def start_dt(m, now):
    h, mi = map(int, m["start_time"].split(":"))
    return now.replace(hour=h, minute=mi, second=0, microsecond=0)


def end_dt(m, now):
    h, mi = map(int, m["end_time"].split(":"))
    return now.replace(hour=h, minute=mi, second=0, microsecond=0)


def admit_timeout(m):
    """Сколько секунд ждать, пока впустят: до конца пары (минимум минута)."""
    left = (end_dt(m, datetime.now(KYIV)) - datetime.now(KYIV)).total_seconds()
    return max(60, left)


def pick_due(meetings, now):
    today = DAYS[now.weekday()]
    due = []
    for m in meetings:
        if m.get("day", "").lower() != today:
            continue
        s = start_dt(m, now)
        if s - EARLY <= now <= s + LATE and now < end_dt(m, now):
            due.append(m)
    return due


# ======================= БРАУЗЕР =======================
def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--lang=en-US")
    opts.add_argument(f"--user-agent={USER_AGENT}")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    # автоматически разрешает доступ к микро/камере (фейковые устройства)
    opts.add_argument("--use-fake-ui-for-media-stream")
    opts.add_argument("--use-fake-device-for-media-stream")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    return webdriver.Chrome(options=opts)


def click_any(driver, xpaths, timeout=0):
    end = time.time() + timeout
    while True:
        for xp in xpaths:
            try:
                for el in driver.find_elements(By.XPATH, xp):
                    if el.is_displayed():
                        el.click()
                        return True
            except Exception:
                pass
        if time.time() >= end:
            return False
        time.sleep(0.5)


def exists_any(driver, xpaths, timeout=0):
    end = time.time() + timeout
    while True:
        for xp in xpaths:
            try:
                if any(e.is_displayed() for e in driver.find_elements(By.XPATH, xp)):
                    return True
            except Exception:
                pass
        if time.time() >= end:
            return False
        time.sleep(1)


def find_visible(driver, xpaths):
    """Первый видимый элемент по любому из xpath (или None)."""
    for xp in xpaths:
        try:
            for el in driver.find_elements(By.XPATH, xp):
                if el.is_displayed():
                    return el
        except Exception:
            pass
    return None


def shot(driver, caption):
    try:
        send_photo(driver.get_screenshot_as_png(), caption)
    except Exception:
        pass


# ======================= ВХОД В GOOGLE =======================
# У формы входа Google поле почты часто type="text", а не "email" —
# поэтому ищем ещё и по id / name / autocomplete.
EMAIL_XP = [
    "//input[@id='identifierId']",
    "//input[@name='identifier']",
    "//input[@autocomplete='username']",
    "//input[@type='email']",
]
PASS_XP = [
    "//input[@name='Passwd']",
    "//input[@name='password']",
    "//input[@autocomplete='current-password']",
    "//input[@type='password']",
]


CAPTCHA_XP = [
    "//input[@name='ca']",
    "//input[@id='ca']",
    "//input[contains(@aria-label,'Type the text')]",
    "//input[contains(@aria-label,'Введіть текст')]",
]


def solve_captcha(driver, tries=3):
    """Если Google показал капчу — шлёт скриншот в Telegram, ждёт ответ текстом и вводит его.
    Возвращает True, если капчи нет или она пройдена."""
    for attempt in range(tries):
        if not exists_any(driver, CAPTCHA_XP, timeout=3):
            return True
        offset = get_update_offset()
        shot(driver, "🔤 Google просит капчу. Ответь сюда текстом с картинки (3 мин)")
        text = wait_for_reply(offset, timeout=180)
        if not text:
            send_telegram("⌛ Капчу не дождался")
            return False
        box = find_visible(driver, CAPTCHA_XP)
        if not box:
            return True
        box.click()
        box.send_keys(text)
        box.send_keys(Keys.ENTER)
        time.sleep(4)
    return not exists_any(driver, CAPTCHA_XP, timeout=2)


def is_logged_in(driver):
    driver.get("https://myaccount.google.com/?hl=en")
    time.sleep(3)
    url = driver.current_url
    return "myaccount.google.com" in url and "signin" not in url and "accounts.google.com" not in url


def login_with_cookies(driver):
    """Вход по cookies из секрета GOOGLE_COOKIES. Возвращает True, если вошли."""
    raw = os.getenv("GOOGLE_COOKIES", "").strip()
    if not raw:
        return False
    try:
        cookies = json.loads(raw)
        driver.get("https://accounts.google.com/")
        time.sleep(2)
        added = 0
        for c in cookies:
            cookie = {k: v for k, v in c.items()
                      if k in ("name", "value", "domain", "path", "secure", "httpOnly", "expiry")}
            if "expiry" in cookie:
                try:
                    cookie["expiry"] = int(cookie["expiry"])
                except Exception:
                    cookie.pop("expiry", None)
            try:
                driver.add_cookie(cookie)
                added += 1
            except Exception:
                # cookie чужого домена (например .youtube.com) — пропускаем
                pass
        logger.info(f"[Google] cookies добавлено: {added}/{len(cookies)}")
        logged = is_logged_in(driver)
        logger.info(f"[Google] вход по cookies: {'да' if logged else 'нет'}")
        if not logged:
            shot(driver, "🔍 Google: cookies не подошли (устарели?)")
        return logged
    except Exception as e:
        logger.error(f"[Google] ошибка cookies: {type(e).__name__}")
        return False


def login_google(driver):
    """Вход в Google. Сначала cookies, потом почта и пароль. True, если вошли."""
    if login_with_cookies(driver):
        return True

    email = os.getenv("GOOGLE_EMAIL", "").strip()
    password = os.getenv("GOOGLE_PASSWORD", "")
    if not email or not password:
        logger.warning("GOOGLE_EMAIL / GOOGLE_PASSWORD не заданы — зайдём как гость")
        return False

    try:
        driver.get(
            "https://accounts.google.com/signin/v2/identifier"
            "?hl=en&flowName=GlifWebSignIn&flowEntry=ServiceLogin"
        )
        if not exists_any(driver, EMAIL_XP, timeout=20):
            shot(driver, "🔍 Google: нет поля почты")
            return False
        box = find_visible(driver, EMAIL_XP)
        box.click()
        box.send_keys(email)
        box.send_keys(Keys.ENTER)
        time.sleep(3)
        if not solve_captcha(driver):
            return False

        if not exists_any(driver, PASS_XP, timeout=25):
            logger.info(f"[Google] нет поля пароля, url: {driver.current_url}")
            shot(driver, f"🔍 Google: после почты нет пароля ({driver.current_url[:120]})")
            return False
        time.sleep(1.5)
        box = find_visible(driver, PASS_XP)
        box.click()
        box.send_keys(password)
        box.send_keys(Keys.ENTER)
        time.sleep(6)
        if not solve_captcha(driver):
            return False
        time.sleep(3)

        url = driver.current_url
        logger.info(f"[Google] после пароля url: {url}")
        if any(x in url for x in ("challenge", "rejected", "deniedsigninrejected")) \
                or "accounts.google.com/signin" in url:
            shot(driver, f"🔍 Google не пустил ({url[:120]})")
            return False

        logged = is_logged_in(driver)
        logger.info(f"[Google] вход: {'да' if logged else 'нет'}, url: {driver.current_url}")
        if not logged:
            shot(driver, f"🔍 Google: вход не подтвердился ({driver.current_url[:120]})")
        return logged
    except Exception as e:
        logger.error(f"[Google] ошибка входа: {type(e).__name__}")
        shot(driver, f"🔍 Google: ошибка входа {type(e).__name__}")
        return False


# ======================= GOOGLE MEET =======================
MIC_OFF = [
    "//*[@role='button' and (contains(@aria-label,'Turn off microphone') or contains(@aria-label,'Вимкнути мікрофон'))]",
]
CAM_OFF = [
    "//*[@role='button' and (contains(@aria-label,'Turn off camera') or contains(@aria-label,'Вимкнути камеру'))]",
]
NAME_INPUT = "//input[@placeholder='Your name' or contains(@placeholder,'ім')]"
MEET_JOIN = [
    "//span[contains(text(),'Join now')]",
    "//span[contains(text(),'Ask to join')]",
    "//span[contains(text(),'Приєднатися')]",
    "//span[contains(text(),'Попросити')]",
    "//span[contains(text(),'Запросити')]",
]
MEET_IN_CALL = [
    "//*[contains(@aria-label,'Leave call')]",
    "//*[contains(@aria-label,'Покинути')]",
    "//*[contains(@aria-label,'Вийти')]",
]


def join_meet(m, driver, subject):
    driver.get(m["url"])
    if not exists_any(driver, MEET_JOIN, timeout=45):
        shot(driver, f"⚠️ <b>Meet</b>: нет кнопки входа в «{subject}»")
        return False

    click_any(driver, MIC_OFF, timeout=3)
    click_any(driver, CAM_OFF, timeout=3)

    # гостевой вход — нужно имя
    try:
        for el in driver.find_elements(By.XPATH, NAME_INPUT):
            if el.is_displayed():
                el.clear()
                el.send_keys(m.get("display_name", "Student"))
    except Exception:
        pass

    if not click_any(driver, MEET_JOIN, timeout=5):
        shot(driver, f"⚠️ <b>Meet</b>: не нажалась кнопка входа в «{subject}»")
        return False
    logger.info("[Meet] запрос на вход отправлен")

    send_telegram(f"⏳ <b>Meet</b>: в очереди на вход в «{subject}»")
    if exists_any(driver, MEET_IN_CALL, timeout=admit_timeout(m)):
        return True
    shot(driver, f"⚠️ <b>Meet</b>: так и не впустили в «{subject}» до конца пары")
    return False


# ======================= ZOOM =======================
ZOOM_IN_CALL = [
    "//*[contains(@class,'footer__leave-btn')]",
    "//button[contains(@aria-label,'Leave')]",
    "//button[contains(@aria-label,'Покинути')]",
]


def join_zoom(m, driver, subject):
    meeting_id = str(m["meeting_id"]).replace(" ", "")
    url = f"https://zoom.us/wc/join/{meeting_id}"
    if m.get("password"):
        url += f"?pwd={m['password']}"
    driver.get(url)
    time.sleep(6)

    click_any(driver, ["//button[@id='onetrust-accept-btn-handler']"], timeout=3)
    click_any(driver, ["//button[@id='wc_agree1']"], timeout=2)

    if not exists_any(driver, ["//input[@id='input-for-name']"], timeout=30):
        shot(driver, f"⚠️ <b>Zoom</b>: нет поля имени в «{subject}»")
        return False
    name = driver.find_element(By.ID, "input-for-name")
    name.clear()
    name.send_keys(m.get("display_name", "Student"))

    if not click_any(
        driver,
        [
            "//button[contains(@class,'preview-join-button')]",
            "//button[contains(text(),'Join')]",
            "//button[contains(text(),'Приєднатися')]",
        ],
        timeout=15,
    ):
        shot(driver, f"⚠️ <b>Zoom</b>: нет кнопки Join в «{subject}»")
        return False
    logger.info("[Zoom] запрос на вход отправлен")

    send_telegram(f"⏳ <b>Zoom</b>: в очереди на вход в «{subject}»")
    if exists_any(driver, ZOOM_IN_CALL, timeout=admit_timeout(m)):
        return True
    shot(driver, f"⚠️ <b>Zoom</b>: так и не вошли в «{subject}» до конца пары")
    return False


# ======================= ПРОЦЕСС ОДНОЙ ПАРЫ =======================
def stay_until_end(m):
    end = end_dt(m, datetime.now(KYIV))
    while True:
        left = (end - datetime.now(KYIV)).total_seconds()
        if left <= 0:
            return
        time.sleep(min(30, left))


def wait_for_start(m):
    s = start_dt(m, datetime.now(KYIV)) - JOIN_LEAD
    left = (s - datetime.now(KYIV)).total_seconds()
    if left > 0:
        logger.info(f"Ждём момента входа: {left:.0f} сек")
        time.sleep(left)


def run_meeting(m, test=False):
    subject = m.get("subject", "без названия")
    platform = m["platform"]
    label = f"{platform} {m.get('day')} {m.get('start_time')}"
    logger.info(f"Старт: {label}")

    driver = None
    joined = False
    try:
        if not test:
            wait_for_start(m)
        driver = make_driver()

        if platform == "google_meet":
            if not login_google(driver):
                send_telegram("⚠️ Google: вход не подтвердился, захожу гостем")
            joined = join_meet(m, driver, subject)
        elif platform == "zoom":
            joined = join_zoom(m, driver, subject)
        else:
            logger.warning(f"Неизвестная платформа: {platform}")
            return

        if joined:
            send_telegram(f"✅ <b>{platform}</b>: на паре «{subject}»\n⏰ до {m['end_time']}")
            stay_until_end(m)
    except Exception as e:
        logger.error(f"Ошибка ({label}): {type(e).__name__}")
        if driver:
            shot(driver, f"❌ Ошибка в «{subject}»: {type(e).__name__}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        if joined:
            send_telegram(f"🚪 <b>{platform}</b>: вышел из «{subject}»")
        logger.info(f"Завершено: {label}")


# ======================= MAIN =======================
def main():
    meetings = load_schedule()
    now = datetime.now(KYIV)
    test = False

    test_idx = os.getenv("TEST_INDEX", "").strip()
    test_url = os.getenv("TEST_URL", "").strip()
    if test_url:
        due = [{
            "platform": "google_meet",
            "subject": "Тест",
            "url": test_url,
            "day": DAYS[now.weekday()],
            "start_time": now.strftime("%H:%M"),
            "end_time": (now + timedelta(minutes=5)).strftime("%H:%M"),
            "display_name": "Test",
        }]
        test = True
    elif test_idx:
        m = dict(meetings[int(test_idx)])
        m["end_time"] = (now + timedelta(minutes=3)).strftime("%H:%M")
        due = [m]
        test = True
    else:
        due = pick_due(meetings, now)

    if not due:
        logger.info(f"Сейчас ({now:%a %H:%M} Киев) подходящих пар нет — выходим")
        return

    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=run_meeting, args=(m, test)) for m in due]
    for p in procs:
        p.start()
    for p in procs:
        p.join()


if __name__ == "__main__":
    main()
