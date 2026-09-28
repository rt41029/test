"""
Бот для GitHub Actions.
Запускается по cron, смотрит расписание (Киев), заходит на пары, которые
начинаются в ближайшие 20 минут или начались не более 25 минут назад,
и сидит до конца пары. Вход в Google — по cookies (если есть) или по почте и паролю.

Кнопки в Telegram (под сообщениями бота):
  📸 Скрин        — свежий скриншот браузера
  ℹ️ Статус       — в звонке или нет, сколько осталось до конца пары
  ⏱ +15 мин      — продлить пару (если препод затянул)
  🔄 Перезайти    — зайти во встречу заново (после вылета / удаления / зависания)
  🚪 Выйти        — выйти со встречи и закрыть браузер

Переменные окружения (Secrets):
  GOOGLE_EMAIL        почта Google-аккаунта
  GOOGLE_PASSWORD     пароль Google-аккаунта
  GOOGLE_COOKIES      (необязательно) JSON со списком cookies Google — пробуется первым
  SCHEDULE_JSON       содержимое schedule.json (если нет локального файла)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  TEST_INDEX          (необязательно) номер пары в списке — зайти на неё прямо
                      сейчас на 3 минуты, для проверки
  TEST_URL            (необязательно) ссылка на Meet — зайти на 5 минут
  FORCE_INDEX         (ставит меню-бот) номер пары — зайти прямо сейчас и сидеть до конца пары
  FORCE_URL           (ставит меню-бот) ссылка на Meet — зайти прямо сейчас на FORCE_LEN
"""
import json
import os
import queue
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

from notifier import (
    send_telegram, send_photo, answer_callback,
    get_update_offset, get_updates,
)

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
EXTEND = timedelta(minutes=15)  # на сколько продлевает кнопка «+15 мин»
FORCE_LEN = timedelta(minutes=90)  # сколько сидим при принудительном входе, если пара уже закончилась / нет времени конца

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# ======================= КНОПКИ TELEGRAM =======================
BTN = {
    "shot": "📸 Скрин",
    "status": "ℹ️ Статус",
    "plus": "⏱ +15 мин",
    "rejoin": "🔄 Перезайти",
    "leave": "🚪 Выйти",
}


def buttons(idx, *acts):
    """Клавиатура под сообщением. callback_data = действие|id запуска|номер пары."""
    run = os.getenv("RUN_ID", "0")
    items = [(BTN[a], f"{a}|{run}|{idx}") for a in acts]
    return [items[i:i + 3] for i in range(0, len(items), 3)]


class Ctx:
    """Всё, что нужно процессу одной пары."""

    def __init__(self, m, idx, inbox):
        self.m = m
        self.idx = idx
        self.inbox = inbox
        self.driver = None
        self.subject = m.get("subject", "без названия")
        self.platform = m["platform"]

    def btn(self, *acts):
        return buttons(self.idx, *acts)

    def in_call_xp(self):
        return MEET_IN_CALL if self.platform == "google_meet" else ZOOM_IN_CALL


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


def seconds_left(m):
    return (end_dt(m, datetime.now(KYIV)) - datetime.now(KYIV)).total_seconds()


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
    # автоматически отвечает на запрос доступа к микро/камере (устройства не подменяем)
    opts.add_argument("--use-fake-ui-for-media-stream")
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


def shot(driver, caption, btns=None):
    """Скриншот в Telegram. Если скрин не получился — хотя бы текст с кнопками."""
    try:
        send_photo(driver.get_screenshot_as_png(), caption, btns)
    except Exception:
        send_telegram(caption, btns)


# ======================= КОМАНДЫ С КНОПОК =======================
def drain(q):
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass


def wait_text(q, timeout):
    """Ждёт текстовое сообщение (например, ответ на капчу)."""
    end = time.time() + timeout
    while True:
        left = end - time.time()
        if left <= 0:
            return None
        try:
            kind, val = q.get(timeout=left)
        except queue.Empty:
            return None
        if kind == "text":
            return val


def present_any(driver, xpaths):
    """Элемент есть на странице (панель управления может быть скрыта, поэтому видимость не проверяем)."""
    for xp in xpaths:
        try:
            if driver.find_elements(By.XPATH, xp):
                return True
        except Exception:
            pass
    return False


def send_status(ctx):
    in_call = False
    if ctx.driver:
        try:
            in_call = present_any(ctx.driver, ctx.in_call_xp())
        except Exception:
            pass
    mins = max(0, int(seconds_left(ctx.m) // 60))
    send_telegram(
        f"ℹ️ <b>{ctx.platform}</b> «{ctx.subject}»\n"
        f"{'🟢 в звонке' if in_call else '🔴 не в звонке'}\n"
        f"до конца пары: {mins} мин (до {ctx.m['end_time']})",
        ctx.btn("shot", "status", "plus", "rejoin", "leave"),
    )


def extend(ctx):
    e = end_dt(ctx.m, datetime.now(KYIV)) + EXTEND
    ctx.m["end_time"] = e.strftime("%H:%M")
    send_telegram(f"⏱ Пара продлена до {ctx.m['end_time']}")


def wait_cmd(ctx, timeout):
    """Ждёт нажатие кнопки до timeout секунд.
    Скрин / статус / продление выполняет сама и продолжает ждать.
    Возвращает 'rejoin' или 'leave', либо None, если время вышло."""
    end = time.time() + timeout
    while True:
        left = end - time.time()
        if left <= 0:
            return None
        try:
            kind, val = ctx.inbox.get(timeout=left)
        except queue.Empty:
            return None
        if kind != "cb":
            continue
        if val == "shot":
            if ctx.driver:
                shot(ctx.driver, f"📸 «{ctx.subject}»", ctx.btn("status", "plus", "rejoin", "leave"))
        elif val == "status":
            send_status(ctx)
        elif val == "plus":
            extend(ctx)
        elif val in ("rejoin", "leave"):
            return val


def wait_admit(ctx, fail_text):
    """Ждёт, пока впустят (до конца пары). Параллельно слушает кнопки.
    Возвращает 'ok' / 'fail' / 'rejoin' / 'leave'."""
    while seconds_left(ctx.m) > 0:
        if exists_any(ctx.driver, ctx.in_call_xp(), timeout=0):
            return "ok"
        act = wait_cmd(ctx, 3)
        if act:
            return act
    shot(ctx.driver, fail_text, ctx.btn("rejoin", "leave"))
    return "fail"


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


def solve_captcha(driver, inbox, tries=3):
    """Если Google показал капчу — шлёт скриншот в Telegram, ждёт ответ текстом и вводит его.
    Возвращает True, если капчи нет или она пройдена."""
    for attempt in range(tries):
        if not exists_any(driver, CAPTCHA_XP, timeout=3):
            return True
        drain(inbox)
        shot(driver, "🔤 Google просит капчу. Ответь сюда текстом с картинки (3 мин)")
        text = wait_text(inbox, 180)
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
        failed = []
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
                failed.append(f"{c.get('name')}@{c.get('domain')}")
        logger.info(f"[Google] cookies добавлено: {added}/{len(cookies)}")
        logger.info(f"[Google] не добавлены (только имена): {failed}")
        logged = is_logged_in(driver)
        logger.info(f"[Google] вход по cookies: {'да' if logged else 'нет'}")
        if not logged:
            shot(driver, "🔍 Google: cookies не подошли (устарели?)")
        return logged
    except Exception as e:
        logger.error(f"[Google] ошибка cookies: {type(e).__name__}")
        return False


def login_google(driver, inbox):
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
        if not solve_captcha(driver, inbox):
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
        if not solve_captcha(driver, inbox):
            return False
        time.sleep(3)

        url = driver.current_url
        logger.info(f"[Google] после пароля url: {url}")
        if "challenge" in url:
            # «Verify it's you» — подтверждение на телефоне, ждём
            shot(driver, "📱 Google просит подтвердить вход. Открой телефон, нажми Да и выбери число с картинки (4 мин)")
            end = time.time() + 240
            while time.time() < end and "challenge" in driver.current_url:
                time.sleep(3)
            url = driver.current_url
            logger.info(f"[Google] после подтверждения url: {url}")
            if "challenge" in url:
                shot(driver, f"🔍 Подтверждение не дождался ({url[:120]})")
                return False
            time.sleep(3)
        if any(x in url for x in ("rejected", "deniedsigninrejected")) \
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
    "//*[@role='button' and (contains(@aria-label,'Turn off microphone') or contains(@aria-label,'Отключить микрофон') or contains(@aria-label,'Вимкнути мікрофон'))]",
]
CAM_OFF = [
    "//*[@role='button' and (contains(@aria-label,'Turn off camera') or contains(@aria-label,'Отключить камеру') or contains(@aria-label,'Вимкнути камеру'))]",
]
NAME_INPUT = "//input[@placeholder='Your name' or contains(@placeholder,'ім') or contains(@placeholder,'Ваше имя')]"
DISMISS_POPUP = [
    "//span[contains(text(),'Не сейчас')]",
    "//span[contains(text(),'Не зараз')]",
    "//span[contains(text(),'Not now')]",
]
MEET_JOIN = [
    "//span[contains(text(),'Join now')]",
    "//span[contains(text(),'Ask to join')]",
    "//span[contains(text(),'Присоедин')]",
    "//span[contains(text(),'Приєднатися')]",
    "//span[contains(text(),'Попросить')]",
    "//span[contains(text(),'Попроситися')]",
    "//span[contains(text(),'Запросити')]",
]
MEET_IN_CALL = [
    "//*[contains(@aria-label,'Leave call')]",
    "//*[contains(@aria-label,'Покин')]",
    "//*[contains(@aria-label,'Выйти')]",
    "//*[contains(@aria-label,'Вийти')]",
]


def join_meet(ctx):
    """Возвращает 'ok' / 'fail' / 'rejoin' / 'leave'."""
    m, driver, subject = ctx.m, ctx.driver, ctx.subject
    url = m["url"]
    if "hl=" not in url:
        url += ("&" if "?" in url else "?") + "hl=en"
    driver.get(url)
    if not exists_any(driver, MEET_JOIN, timeout=45):
        shot(driver, f"⚠️ <b>Meet</b>: нет кнопки входа в «{subject}»", ctx.btn("rejoin", "leave"))
        return "fail"

    click_any(driver, DISMISS_POPUP, timeout=3)   # окно «Получать уведомления»
    click_any(driver, MIC_OFF, timeout=3)         # микрофон и камера выключены
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
        shot(driver, f"⚠️ <b>Meet</b>: не нажалась кнопка входа в «{subject}»", ctx.btn("rejoin", "leave"))
        return "fail"
    logger.info("[Meet] запрос на вход отправлен")

    send_telegram(f"⏳ <b>Meet</b>: в очереди на вход в «{subject}»", ctx.btn("shot", "rejoin", "leave"))
    return wait_admit(ctx, f"⚠️ <b>Meet</b>: так и не впустили в «{subject}» до конца пары")


# ======================= ZOOM =======================
ZOOM_IN_CALL = [
    "//*[contains(@class,'footer__leave-btn')]",
    "//button[contains(@aria-label,'Leave')]",
    "//button[contains(@aria-label,'Покинути')]",
]


def join_zoom(ctx):
    """Возвращает 'ok' / 'fail' / 'rejoin' / 'leave'."""
    m, driver, subject = ctx.m, ctx.driver, ctx.subject
    meeting_id = str(m["meeting_id"]).replace(" ", "")
    url = f"https://zoom.us/wc/join/{meeting_id}"
    if m.get("password"):
        url += f"?pwd={m['password']}"
    driver.get(url)
    time.sleep(6)

    click_any(driver, ["//button[@id='onetrust-accept-btn-handler']"], timeout=3)
    click_any(driver, ["//button[@id='wc_agree1']"], timeout=2)

    if not exists_any(driver, ["//input[@id='input-for-name']"], timeout=30):
        shot(driver, f"⚠️ <b>Zoom</b>: нет поля имени в «{subject}»", ctx.btn("rejoin", "leave"))
        return "fail"
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
        shot(driver, f"⚠️ <b>Zoom</b>: нет кнопки Join в «{subject}»", ctx.btn("rejoin", "leave"))
        return "fail"
    logger.info("[Zoom] запрос на вход отправлен")

    send_telegram(f"⏳ <b>Zoom</b>: в очереди на вход в «{subject}»", ctx.btn("shot", "rejoin", "leave"))
    return wait_admit(ctx, f"⚠️ <b>Zoom</b>: так и не вошли в «{subject}» до конца пары")


# ======================= ПРОЦЕСС ОДНОЙ ПАРЫ =======================
def detect_reason(driver):
    try:
        text = driver.find_element(By.TAG_NAME, "body").text.lower()
    except Exception:
        return "браузер не отвечает"
    if any(k in text for k in ("removed", "удалил", "видалил", "вилучен")):
        return "похоже, вас удалили из встречи"
    if any(k in text for k in ("ended", "завершен", "завершил", "завершено", "закінчен")):
        return "похоже, встреча завершена"
    if any(k in text for k in ("rejoin", "вернуться", "повернутися", "return to home", "главный экран", "головний екран")):
        return "вы вышли из встречи"
    return "причина неизвестна"


def monitor_meeting(ctx):
    """Сидит до конца пары, следит что мы в звонке и слушает кнопки.
    Возвращает 'done' / 'kicked' / 'rejoin' / 'leave'."""
    in_call = ctx.in_call_xp()
    misses = 0
    while True:
        left = seconds_left(ctx.m)
        if left <= 0:
            return "done"
        act = wait_cmd(ctx, min(20, left))
        if act:
            return act
        if seconds_left(ctx.m) <= 0:
            return "done"
        ok = present_any(ctx.driver, in_call)
        misses = 0 if ok else misses + 1
        if misses >= 2:  # две проверки подряд, чтобы не реагировать на мигание интерфейса
            reason = detect_reason(ctx.driver)
            try:
                logger.info(f"[{ctx.platform}] url при выходе: {ctx.driver.current_url}")
            except Exception:
                pass
            logger.warning(f"[{ctx.platform}] вылетели из «{ctx.subject}»: {reason}")
            shot(
                ctx.driver,
                f"🚫 <b>{ctx.platform}</b>: вышло из «{ctx.subject}» до конца пары\n{reason}",
                ctx.btn("rejoin", "shot", "leave"),
            )
            return "kicked"


def wait_for_start(m):
    s = start_dt(m, datetime.now(KYIV)) - JOIN_LEAD
    left = (s - datetime.now(KYIV)).total_seconds()
    if left > 0:
        logger.info(f"Ждём момента входа: {left:.0f} сек")
        time.sleep(left)


def run_meeting(m, idx, inbox, test=False):
    ctx = Ctx(m, idx, inbox)
    subject, platform = ctx.subject, ctx.platform
    label = f"{platform} {m.get('day')} {m.get('start_time')}"
    logger.info(f"Старт: {label}")

    if platform not in ("google_meet", "zoom"):
        logger.warning(f"Неизвестная платформа: {platform}")
        return

    final = None  # 'finished' / 'left'
    try:
        if not test:
            wait_for_start(m)
        ctx.driver = make_driver()

        if platform == "google_meet" and not login_google(ctx.driver, inbox):
            send_telegram("⚠️ Google: вход не подтвердился, захожу гостем")

        while seconds_left(m) > 0:
            res = join_meet(ctx) if platform == "google_meet" else join_zoom(ctx)

            if res == "ok":
                shot(
                    ctx.driver,
                    f"✅ <b>{platform}</b>: на паре «{subject}»\n⏰ до {m['end_time']}",
                    ctx.btn("shot", "status", "plus", "rejoin", "leave"),
                )
                res = monitor_meeting(ctx)

            if res == "done":
                final = "finished"
                break
            if res == "leave":
                final = "left"
                break
            if res == "rejoin":
                send_telegram("🔄 Перезахожу…")
                continue

            # 'fail' или 'kicked': держим браузер и ждём кнопку до конца пары
            act = wait_cmd(ctx, max(0, seconds_left(m)))
            if act == "rejoin":
                send_telegram("🔄 Перезахожу…")
                continue
            if act == "leave":
                final = "left"
            break
    except Exception as e:
        logger.error(f"Ошибка ({label}): {type(e).__name__}")
        if ctx.driver:
            shot(ctx.driver, f"❌ Ошибка в «{subject}»: {type(e).__name__}", ctx.btn("shot"))
    finally:
        if ctx.driver:
            try:
                ctx.driver.quit()
            except Exception:
                pass
        if final == "finished":
            send_telegram(f"🚪 <b>{platform}</b>: пара «{subject}» закончилась, вышел")
        elif final == "left":
            send_telegram(f"🚪 <b>{platform}</b>: вышел из «{subject}» по кнопке")
        logger.info(f"Завершено: {label}")


# ======================= MAIN =======================
def main():
    meetings = load_schedule()
    now = datetime.now(KYIV)
    test = False

    test_idx = os.getenv("TEST_INDEX", "").strip()
    test_url = os.getenv("TEST_URL", "").strip()
    force_idx = os.getenv("FORCE_INDEX", "").strip()
    force_url = os.getenv("FORCE_URL", "").strip()

    def force_end():
        # не переходим через полночь: end_dt считает время в рамках текущей даты
        return min(now + FORCE_LEN, now.replace(hour=23, minute=59, second=0, microsecond=0))

    if force_url:
        due = [{
            "platform": "google_meet",
            "subject": "Ссылка из меню",
            "url": force_url,
            "day": DAYS[now.weekday()],
            "start_time": now.strftime("%H:%M"),
            "end_time": force_end().strftime("%H:%M"),
            "display_name": "Student",
        }]
        test = True
    elif force_idx:
        m = dict(meetings[int(force_idx)])
        end = end_dt(m, now)
        if end <= now + timedelta(minutes=10):  # пара уже закончилась или вот-вот закончится
            end = force_end()
        m["day"] = DAYS[now.weekday()]
        m["start_time"] = now.strftime("%H:%M")
        m["end_time"] = end.strftime("%H:%M")
        due = [m]
        test = True
    elif test_url:
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

    # id запуска: кнопки от прошлых запусков будут отвечать «неактуально»
    run_id = str(int(time.time()) % 1000000)
    os.environ["RUN_ID"] = run_id
    offset = get_update_offset() or 0

    ctx = multiprocessing.get_context("spawn")
    queues = [ctx.Queue() for _ in due]
    procs = [ctx.Process(target=run_meeting, args=(m, i, queues[i], test))
             for i, m in enumerate(due)]
    for p in procs:
        p.start()

    # Главный процесс — единственный, кто читает Telegram, и раздаёт события по парам
    while any(p.is_alive() for p in procs):
        events, offset = get_updates(offset, timeout=8)
        for kind, cid, val in events:
            if kind == "text":
                for i, p in enumerate(procs):
                    if p.is_alive():
                        queues[i].put(("text", val))
                continue
            try:
                act, run, i = val.split("|")
                i = int(i)
            except ValueError:
                answer_callback(cid, "?")
                continue
            if run != run_id or i >= len(procs) or not procs[i].is_alive():
                answer_callback(cid, "Эта кнопка уже неактуальна")
                continue
            answer_callback(cid, "Принято")
            queues[i].put(("cb", act))

    for p in procs:
        p.join()


if __name__ == "__main__":
    main()
