"""
Meet/Zoom бот для GitHub Actions (Киевское время).

Что умеет:
  • Сам встаёт в очередь на вход за JOIN_LEAD (5) минут до начала пары.
  • Вход в Google: cookies → клик по своей плашке на «Choose an account»
    (в т.ч. Signed out) → email+пароль → капча (решается текстом из Telegram)
    → 2FA (число само извлекается и присылается в Telegram).
  • Вылетел со встречи — автоматически перезаходит (до AUTO_REJOIN раз),
    дальше решается кнопками.
  • Всё решается из Telegram: кнопки 📸 ℹ️ ⏱ 🔄 🚪 + ответ текстом на капчу.

Secrets GitHub:
  GOOGLE_EMAIL, GOOGLE_PASSWORD, GOOGLE_COOKIES (опц. JSON),
  SCHEDULE_JSON (опц., иначе schedule.json из репо),
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Inputs workflow:
  TEST_URL / TEST_INDEX (проверка), FORCE_URL / FORCE_INDEX (принудительно).
"""
import json
import os
import queue
import re
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

DAYS = ["monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday"]

EARLY = timedelta(minutes=20)       # за сколько до начала пара считается «своей»
LATE = timedelta(minutes=25)        # насколько можно опоздать (задержка cron)
PREP_LEAD = timedelta(minutes=8)    # за сколько до пары запускаем браузер и логин
JOIN_LEAD = timedelta(minutes=5)    # за сколько до пары жмём «Join/Ask to join»
EXTEND = timedelta(minutes=15)      # на сколько продлевает «⏱ +15 мин»
FORCE_LEN = timedelta(minutes=90)
AUTO_REJOIN = 2                     # автоперезаходов при вылете до ожидания кнопок
QUEUE_UPDATE = 120                  # сек: как часто шлём скрин «в очереди»

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
    """callback_data = действие|id запуска|номер пары."""
    run = os.getenv("RUN_ID", "0")
    items = [(BTN[a], f"{a}|{run}|{idx}") for a in acts]
    return [items[i:i + 3] for i in range(0, len(items), 3)]


class Ctx:
    """Состояние процесса одной пары."""

    def __init__(self, m, idx, inbox):
        self.m = m
        self.idx = idx
        self.inbox = inbox
        self.driver = None
        self.subject = m.get("subject", "без названия")
        self.platform = m["platform"]
        self.rejoins = 0  # сколько автоперезаходов уже сделали

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


def _t(m, key, now):
    h, mi = map(int, m[key].split(":"))
    return now.replace(hour=h, minute=mi, second=0, microsecond=0)


def start_dt(m, now):
    return _t(m, "start_time", now)


def end_dt(m, now):
    return _t(m, "end_time", now)


def seconds_left(m):
    now = datetime.now(KYIV)
    return (end_dt(m, now) - now).total_seconds()


def pick_due(meetings, now):
    """Пары сегодня, которые идут/вот-вот начнутся. Возвращает [(idx, m)]."""
    today = DAYS[now.weekday()]
    due = []
    for i, m in enumerate(meetings):
        if not m.get("enabled", True):      # тогл из меню-бота (KV)
            continue
        if m.get("day", "").lower() != today:
            continue
        s = start_dt(m, now)
        if s - EARLY <= now <= s + LATE and now < end_dt(m, now):
            due.append((i, m))
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


def present_any(driver, xpaths):
    """Элемент есть в DOM (панель звонка может быть невидима — видимость не важна)."""
    for xp in xpaths:
        try:
            if driver.find_elements(By.XPATH, xp):
                return True
        except Exception:
            pass
    return False


def find_visible(driver, xpaths):
    for xp in xpaths:
        try:
            for el in driver.find_elements(By.XPATH, xp):
                if el.is_displayed():
                    return el
        except Exception:
            pass
    return None


def shot(driver, caption, btns=None):
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
    send_telegram(f"⏱ Пара «{ctx.subject}» продлена до {ctx.m['end_time']}")


def wait_cmd(ctx, timeout):
    """Ждёт кнопку до timeout сек. Скрин/статус/+15 выполняет и ждёт дальше.
    Возвращает 'rejoin' / 'leave' / None."""
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
                shot(ctx.driver, f"📸 «{ctx.subject}»",
                     ctx.btn("status", "plus", "rejoin", "leave"))
        elif val == "status":
            send_status(ctx)
        elif val == "plus":
            extend(ctx)
        elif val in ("rejoin", "leave"):
            return val


def wait_admit(ctx, fail_text):
    """Ждёт, пока впустят, до конца пары. Шлёт периодические скрины очереди.
    Возвращает 'ok' / 'fail' / 'rejoin' / 'leave'."""
    last_upd = 0.0
    while seconds_left(ctx.m) > 0:
        if exists_any(ctx.driver, ctx.in_call_xp(), timeout=0):
            return "ok"
        now = time.time()
        if now - last_upd > QUEUE_UPDATE:
            last_upd = now
            shot(ctx.driver,
                 f"⏳ <b>{ctx.platform}</b>: всё ещё в очереди в «{ctx.subject}» "
                 f"(осталось {max(0, int(seconds_left(ctx.m)//60))} мин до конца)",
                 ctx.btn("shot", "rejoin", "leave"))
        act = wait_cmd(ctx, 3)
        if act:
            return act
    shot(ctx.driver, fail_text, ctx.btn("rejoin", "leave"))
    return "fail"


# ======================= ВХОД В GOOGLE =======================
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
# Капча с текстовым полем (старая форма Google)
CAPTCHA_XP = [
    "//input[@name='ca']",
    "//input[@id='ca']",
    "//input[contains(@aria-label,'Type the text')]",
    "//input[contains(@aria-label,'Введіть текст')]",
    "//input[contains(@aria-label,'Введите текст')]",
]
# reCAPTCHA: фрейм + чекбокс «Я не робот»
RECAPTCHA_FRAME = "//iframe[contains(@title,'reCAPTCHA') or contains(@src,'recaptcha')]"
RECAPTCHA_BOX = ("//span[@id='recaptcha-anchor' and not(contains(@class,'checked'))]"
                 " | //div[@class='recaptcha-checkbox-border']")
# Плашка своего аккаунта на «Choose an account» (data-identifier = email)
PROFILE_XP = lambda email: [
    f"//*[@data-identifier='{email}']",
    f"//*[contains(@aria-label,'{email}')]",
    f"//*[contains(text(),'{email}')]/ancestor::*[@role='link' or @role='button'][1]",
]
# Кнопки «согласен/далее» на неизвестных промежуточных экранах
GENERIC_OK = [
    "//button[.//*[contains(text(),'Далее')]] | //button[contains(text(),'Далее')]",
    "//button[.//*[contains(text(),'Next')]] | //button[contains(text(),'Next')]",
    "//button[contains(text(),'Продолжить') or contains(text(),'Continue')]",
    "//button[contains(text(),'Принимаю') or contains(text(),'I agree')]",
    "//button[contains(text(),'Подтвердить') or contains(text(),'Confirm')]",
    "//button[contains(text(),'Да') or contains(text(),'Yes')]",
]

NUM_PATTERNS = [
    r"(?:Tap|Нажмите|Натисніть|Нажми|нажми|Натисніть)\s+(\d{1,2})",
    r"(?:number|число|цифру|цифру)\s+(\d{1,2})",
    r"\b(\d{2})\b",
]


def extract_2fa_number(driver):
    try:
        text = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        return None
    for p in NUM_PATTERNS:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1)
    return None


def try_recaptcha_checkbox(driver):
    """Пытается ткнуть в чекбокс reCAPTCHA. True — если кликнули."""
    try:
        frames = driver.find_elements(By.XPATH, RECAPTCHA_FRAME)
        for fr in frames:
            try:
                driver.switch_to.frame(fr)
                box = find_visible(driver, [RECAPTCHA_BOX])
                if box:
                    box.click()
                    time.sleep(5)
                    return True
            except Exception:
                pass
            finally:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
    except Exception:
        pass
    return False


def solve_captcha(driver, inbox, tries=3):
    """Капча решается из Telegram: скриншот -> ждём текст -> вводим.
    Возвращает True, если капчи нет или она пройдена."""
    for attempt in range(tries):
        if not exists_any(driver, CAPTCHA_XP, timeout=3):
            return True
        # сначала пробуем галочку «Я не робот» — иногда капча снимается сама
        if try_recaptcha_checkbox(driver):
            if not exists_any(driver, CAPTCHA_XP, timeout=3):
                send_telegram("✅ Капча снялась галочкой «Я не робот»")
                return True
        drain(inbox)
        shot(driver, "🔤 <b>Капча Google</b>\nОтветь СЮДА текстом с картинки (3 мин)")
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
    return "myaccount.google.com" in url and "signin" not in url \
        and "accounts.google.com" not in url


def click_profile_if_chooser(driver, email):
    """Если Google показывает 'Choose an account' (Signed out) — кликает
    по плашке своего аккаунта. True — кликнули."""
    end = time.time() + 8
    while time.time() < end:
        try:
            ids = driver.find_elements(By.XPATH, "//*[@data-identifier]")
            if ids:
                for xp in PROFILE_XP(email):
                    for el in driver.find_elements(By.XPATH, xp):
                        if el.is_displayed():
                            try:
                                el.click()
                                logger.info("[Google] клик по своему профилю (chooser)")
                                time.sleep(2.5)
                                return True
                            except Exception:
                                pass
        except Exception:
            pass
        time.sleep(0.5)
    return False


def click_through_generic(driver, rounds=3):
    """Жмёт «Далее/Принимаю/Continue» на неизвестных экранах Google."""
    for _ in range(rounds):
        if not click_any(driver, GENERIC_OK, timeout=2):
            return
        time.sleep(2)


def login_with_cookies(driver):
    raw = os.getenv("GOOGLE_COOKIES", "").strip()
    if not raw:
        return False
    try:
        cookies = json.loads(raw)
        driver.get("https://accounts.google.com/")
        time.sleep(2)
        added, failed = 0, []
        for c in cookies:
            cookie = {k: v for k, v in c.items()
                      if k in ("name", "value", "domain", "path", "secure",
                               "httpOnly", "expiry")}
            if "expiry" in cookie:
                try:
                    cookie["expiry"] = int(cookie["expiry"])
                except Exception:
                    cookie.pop("expiry", None)
            try:
                driver.add_cookie(cookie)
                added += 1
            except Exception:
                failed.append(f"{c.get('name')}@{c.get('domain')}")
        logger.info(f"[Google] cookies: {added}/{len(cookies)} (не влезли: {failed})")
        logged = is_logged_in(driver)
        logger.info(f"[Google] вход по cookies: {'да' if logged else 'нет'}")
        if not logged:
            shot(driver, "🔍 Google: cookies не подошли — иду по почте/паролю")
        return logged
    except Exception as e:
        logger.error(f"[Google] ошибка cookies: {type(e).__name__}")
        return False


def login_google(driver, inbox):
    """Полный fallback-конвейер. True — вошли."""
    if login_with_cookies(driver):
        return True

    email = os.getenv("GOOGLE_EMAIL", "").strip()
    password = os.getenv("GOOGLE_PASSWORD", "")
    if not email or not password:
        logger.warning("GOOGLE_EMAIL/GOOGLE_PASSWORD не заданы — захожу гостем")
        return False

    try:
        driver.get(
            "https://accounts.google.com/signin/v2/identifier"
            "?hl=en&flowName=GlifWebSignIn&flowEntry=ServiceLogin"
        )
        time.sleep(2)

        # 1) chooser «Choose an account» (часто после слёта cookies)
        if not click_profile_if_chooser(driver, email):
            # 2) обычный ввод email
            if exists_any(driver, EMAIL_XP, timeout=20):
                box = find_visible(driver, EMAIL_XP)
                box.click()
                box.send_keys(email)
                box.send_keys(Keys.ENTER)
                time.sleep(3)
                if not solve_captcha(driver, inbox):
                    return False
                # 3) после email иногда снова chooser
                click_profile_if_chooser(driver, email)
            else:
                click_through_generic(driver)
                if exists_any(driver, EMAIL_XP, timeout=5):
                    box = find_visible(driver, EMAIL_XP)
                    box.click()
                    box.send_keys(email)
                    box.send_keys(Keys.ENTER)
                    time.sleep(3)
                    solve_captcha(driver, inbox)

        # пароль (после клика по плашке Google почти всегда просит пароль)
        if not exists_any(driver, PASS_XP, timeout=25):
            logger.info(f"[Google] нет поля пароля, url: {driver.current_url}")
            click_through_generic(driver)
        if not exists_any(driver, PASS_XP, timeout=5):
            shot(driver, f"🔍 Google: после почты нет пароля ({driver.current_url[:100]})")
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

        # 2FA / «Verify it's you» — число парсим и шлём
        if "challenge" in url:
            time.sleep(2)
            num = extract_2fa_number(driver)
            if num:
                cap = (f"📱 <b>Google 2FA</b>\nПодтверди вход на телефоне. "
                       f"Нажми число: <b>{num}</b>\n(жду 4 мин)")
            else:
                cap = ("📱 <b>Google 2FA</b>\nОткрой телефон и подтверди вход "
                       "(«Да» + число с экрана).\n(жду 4 мин)")
            shot(driver, cap)
            end = time.time() + 240
            while time.time() < end and "challenge" in driver.current_url:
                time.sleep(3)
            url = driver.current_url
            logger.info(f"[Google] после 2FA url: {url}")
            if "challenge" in url:
                shot(driver, f"🔍 2FA не подтвердили ({url[:100]})")
                return False
            time.sleep(3)

        # неизвестные экраны (условия, подтверждения и т.п.) — прокликиваем
        click_through_generic(driver)

        if any(x in url for x in ("rejected", "deniedsigninrejected")) \
                or "accounts.google.com/signin" in url:
            shot(driver, f"🔍 Google не пустил ({url[:100]})")
            return False

        logged = is_logged_in(driver)
        logger.info(f"[Google] вход: {'да' if logged else 'нет'}")
        if not logged:
            shot(driver, f"🔍 Google: вход не подтвердился ({driver.current_url[:100]})")
        return logged
    except Exception as e:
        logger.error(f"[Google] ошибка входа: {type(e).__name__}")
        shot(driver, f"🔍 Google: ошибка входа {type(e).__name__}")
        return False


# ======================= GOOGLE MEET =======================
MIC_OFF = [
    "//*[@role='button' and (contains(@aria-label,'Turn off microphone')"
    " or contains(@aria-label,'Отключить микрофон')"
    " or contains(@aria-label,'Вимкнути мікрофон'))]",
]
CAM_OFF = [
    "//*[@role='button' and (contains(@aria-label,'Turn off camera')"
    " or contains(@aria-label,'Отключить камеру')"
    " or contains(@aria-label,'Вимкнути камеру'))]",
]
NAME_INPUT = ("//input[@placeholder='Your name' or contains(@placeholder,'ім')"
              " or contains(@placeholder,'Ваше имя') or contains(@placeholder,'имя')]")
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
]
MEET_IN_CALL = [
    "//*[contains(@aria-label,'Leave call')]",
    "//*[contains(@aria-label,'Покин')]",
    "//*[contains(@aria-label,'Выйти')]",
    "//*[contains(@aria-label,'Вийти')]",
]


def join_meet(ctx):
    m, driver, subject = ctx.m, ctx.driver, ctx.subject
    url = m["url"]
    if "hl=" not in url:
        url += ("&" if "?" in url else "?") + "hl=en"
    driver.get(url)
    if not exists_any(driver, MEET_JOIN, timeout=45):
        shot(driver, f"⚠️ <b>Meet</b>: нет кнопки входа в «{subject}»",
             ctx.btn("rejoin", "leave"))
        return "fail"

    click_any(driver, DISMISS_POPUP, timeout=3)
    click_any(driver, MIC_OFF, timeout=3)
    click_any(driver, CAM_OFF, timeout=3)

    try:
        for el in driver.find_elements(By.XPATH, NAME_INPUT):
            if el.is_displayed():
                el.clear()
                el.send_keys(m.get("display_name", "Student"))
    except Exception:
        pass

    if not click_any(driver, MEET_JOIN, timeout=5):
        shot(driver, f"⚠️ <b>Meet</b>: не нажалась кнопка входа в «{subject}»",
             ctx.btn("rejoin", "leave"))
        return "fail"
    logger.info("[Meet] запрос на вход отправлен")

    send_telegram(f"⏳ <b>Meet</b>: в очереди на вход в «{subject}»",
                  ctx.btn("shot", "rejoin", "leave"))
    return wait_admit(ctx, f"⚠️ <b>Meet</b>: так и не впустили в «{subject}»")


# ======================= ZOOM =======================
ZOOM_IN_CALL = [
    "//*[contains(@class,'footer__leave-btn')]",
    "//button[contains(@aria-label,'Leave')]",
    "//button[contains(@aria-label,'Покинути')]",
    "//button[contains(@aria-label,'Покинуть')]",
]


def join_zoom(ctx):
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
        shot(driver, f"⚠️ <b>Zoom</b>: нет поля имени в «{subject}»",
             ctx.btn("rejoin", "leave"))
        return "fail"
    name = driver.find_element(By.ID, "input-for-name")
    name.clear()
    name.send_keys(m.get("display_name", "Student"))

    if not click_any(
        driver,
        ["//button[contains(@class,'preview-join-button')]",
         "//button[contains(text(),'Join')]",
         "//button[contains(text(),'Приєднатися')]",
         "//button[contains(text(),'Присоедин')]"],
        timeout=15,
    ):
        shot(driver, f"⚠️ <b>Zoom</b>: нет кнопки Join в «{subject}»",
             ctx.btn("rejoin", "leave"))
        return "fail"
    logger.info("[Zoom] запрос на вход отправлен")

    send_telegram(f"⏳ <b>Zoom</b>: в очереди на вход в «{subject}»",
                  ctx.btn("shot", "rejoin", "leave"))
    return wait_admit(ctx, f"⚠️ <b>Zoom</b>: так и не вошли в «{subject}»")


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
    if any(k in text for k in ("rejoin", "вернуться", "повернутися",
                               "return to home", "главный экран", "головний екран")):
        return "вы вышли из встречи"
    return "причина неизвестна"


def monitor_meeting(ctx):
    """Сидит до конца пары. При вылете — автоперезаход (до AUTO_REJOIN раз),
    потом кнопки. Возвращает 'done' / 'kicked' / 'rejoin' / 'leave'."""
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
        if misses >= 2:
            reason = detect_reason(ctx.driver)
            try:
                logger.info(f"[{ctx.platform}] url при выходе: {ctx.driver.current_url}")
            except Exception:
                pass
            logger.warning(f"[{ctx.platform}] вылетели из «{ctx.subject}»: {reason}")
            if ctx.rejoins < AUTO_REJOIN and "заверш" not in reason \
                    and "ended" not in reason:
                ctx.rejoins += 1
                send_telegram(
                    f"🔄 <b>{ctx.platform}</b>: вылет из «{ctx.subject}» ({reason}).\n"
                    f"Автоперезаход {ctx.rejoins}/{AUTO_REJOIN}…",
                    ctx.btn("shot", "leave"),
                )
                time.sleep(15)
                return "autorejoin"
            shot(
                ctx.driver,
                f"🚫 <b>{ctx.platform}</b>: вышло из «{ctx.subject}» до конца пары\n{reason}",
                ctx.btn("rejoin", "shot", "leave"),
            )
            return "kicked"


def wait_for_prep(m):
    """Спим до момента PREP_LEAD до начала пары (браузер+логин)."""
    s = start_dt(m, datetime.now(KYIV)) - PREP_LEAD
    left = (s - datetime.now(KYIV)).total_seconds()
    if left > 0:
        logger.info(f"Ждём момента подготовки: {left:.0f} сек")
        time.sleep(left)


def wait_for_join(m):
    """Спим до момента JOIN_LEAD до начала пары (клик по Join)."""
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

    final = None
    try:
        if not test:
            wait_for_prep(m)
        ctx.driver = make_driver()
        send_telegram(
            f"🤖 <b>{platform}</b>: готовлюсь к «{subject}» "
            f"(пара {m['start_time']}–{m['end_time']})"
        )

        if platform == "google_meet" and not login_google(ctx.driver, inbox):
            send_telegram("⚠️ Google: вход не подтвердился, захожу гостем")

        if not test:
            wait_for_join(m)

        while seconds_left(m) > 0:
            res = join_meet(ctx) if platform == "google_meet" else join_zoom(ctx)

            if res == "ok":
                ctx.rejoins = 0
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
            if res in ("rejoin", "autorejoin"):
                if res == "rejoin":
                    send_telegram("🔄 Перезахожу…")
                continue

            # 'fail' или 'kicked': ждём кнопку до конца пары
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
            shot(ctx.driver, f"❌ Ошибка в «{subject}»: {type(e).__name__}",
                 ctx.btn("shot"))
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
        return min(now + FORCE_LEN,
                   now.replace(hour=23, minute=59, second=0, microsecond=0))

    if force_url:
        due = [(0, {
            "platform": "google_meet",
            "subject": "Ссылка из меню",
            "url": force_url,
            "day": DAYS[now.weekday()],
            "start_time": now.strftime("%H:%M"),
            "end_time": force_end().strftime("%H:%M"),
            "display_name": "Student",
        })]
        test = True
    elif force_idx:
        i = int(force_idx)
        m = dict(meetings[i])
        end = end_dt(m, now)
        if end <= now + timedelta(minutes=10):
            end = force_end()
        m["day"] = DAYS[now.weekday()]
        m["start_time"] = now.strftime("%H:%M")
        m["end_time"] = end.strftime("%H:%M")
        due = [(i, m)]
        test = True
    elif test_url:
        due = [(0, {
            "platform": "google_meet",
            "subject": "Тест",
            "url": test_url,
            "day": DAYS[now.weekday()],
            "start_time": now.strftime("%H:%M"),
            "end_time": (now + timedelta(minutes=5)).strftime("%H:%M"),
            "display_name": "Test",
        })]
        test = True
    elif test_idx:
        i = int(test_idx)
        m = dict(meetings[i])
        m["end_time"] = (now + timedelta(minutes=3)).strftime("%H:%M")
        due = [(i, m)]
        test = True
    else:
        due = pick_due(meetings, now)

    if not due:
        logger.info(f"Сейчас ({now:%a %H:%M} Киев) подходящих пар нет — выходим")
        return

    # RUN_ID выставляем ДО spawn — env наследуется дочерними процессами
    run_id = str(int(time.time()) % 1000000)
    os.environ["RUN_ID"] = run_id
    offset = get_update_offset() or 0

    send_telegram(
        f"🤖 Запуск бота (run {run_id}): {len(due)} "
        f"пар(ы) — {', '.join(m.get('subject', '?') for _, m in due)}"
    )

    ctx = multiprocessing.get_context("spawn")
    queues = [ctx.Queue() for _ in due]
    procs = [ctx.Process(target=run_meeting, args=(m, i, queues[k], test))
             for k, (i, m) in enumerate(due)]
    for p in procs:
        p.start()

    # Главный процесс — единственный читает Telegram и раздаёт события
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
    send_telegram("🏁 Запуск завершён, браузер закрыт")


if __name__ == "__main__":
    main()
