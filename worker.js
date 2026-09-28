// Cloudflare Worker: меню-бот в Telegram.
// Показывает список пар из расписания и по кнопке запускает workflow на GitHub
// (workflow_dispatch с force_index / force_url), бот сам берёт ссылку из schedule.json.
//
// Secrets / Variables воркера:
//   MENU_BOT_TOKEN    токен ВТОРОГО бота (только для меню)
//   ALLOWED_CHAT_ID   ваш chat id (тот же, что TELEGRAM_CHAT_ID) — остальным бот не отвечает
//   WEBHOOK_SECRET    любая случайная строка из букв/цифр (та же в setWebhook)
//   GH_TOKEN          fine-grained токен GitHub: только этот репозиторий, Actions: Read and write
//   GH_REPO           логин/репозиторий, например ivan/meet-bot
//   SCHEDULE_JSON     то же содержимое, что в секрете SCHEDULE_JSON на GitHub
//   GH_REF            (необязательно) ветка, по умолчанию main
//   GH_WORKFLOW       (необязательно) имя файла workflow, по умолчанию meetings.yml

const DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
const DAY_RU = {
  monday: "Понедельник", tuesday: "Вторник", wednesday: "Среда", thursday: "Четверг",
  friday: "Пятница", saturday: "Суббота", sunday: "Воскресенье",
};
const DAY_SHORT = {
  monday: "Пн", tuesday: "Вт", wednesday: "Ср", thursday: "Чт",
  friday: "Пт", saturday: "Сб", sunday: "Вс",
};

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("ok");
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("ok");
    }
    try {
      await handle(update, env);
    } catch (e) {
      console.log("error", e && e.message);
    }
    return new Response("ok");
  },
};

const tg = (env, method, body) =>
  fetch(`https://api.telegram.org/bot${env.MENU_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => r.json());

function todayKyiv() {
  const fmt = (tz) =>
    new Intl.DateTimeFormat("en-US", { weekday: "long", timeZone: tz }).format(new Date()).toLowerCase();
  try {
    return fmt("Europe/Kyiv");
  } catch {
    return fmt("Europe/Kiev");
  }
}

function menuView(schedule) {
  const present = DAYS.filter((d) => schedule.some((m) => (m.day || "").toLowerCase() === d));
  const rows = [[{ text: "🔥 Сегодня", callback_data: "d|today" }]];
  for (let i = 0; i < present.length; i += 3) {
    rows.push(
      present.slice(i, i + 3).map((d) => ({ text: DAY_SHORT[d], callback_data: `d|${d}` }))
    );
  }
  return {
    text: "📚 Выбери день, потом пару — бот зайдёт на неё прямо сейчас.\nМожно просто прислать ссылку на Google Meet.",
    kb: rows,
  };
}

function dayView(schedule, day) {
  const items = schedule
    .map((m, i) => ({ m, i }))
    .filter((x) => (x.m.day || "").toLowerCase() === day)
    .sort((a, b) => a.m.start_time.localeCompare(b.m.start_time));
  const rows = items.map(({ m, i }) => ({
    text: `${m.platform === "zoom" ? "🔵" : "🟢"} ${m.start_time}–${m.end_time} ${(m.subject || "без названия").slice(0, 32)}`,
    callback_data: `j|${i}`,
  }));
  const kb = rows.map((b) => [b]);
  kb.push([{ text: "◀️ Меню", callback_data: "m" }]);
  return {
    text: items.length ? `${DAY_RU[day]} — выбери пару:` : `${DAY_RU[day]}: пар нет`,
    kb,
  };
}

async function show(env, chat, cq, view) {
  const markup = { inline_keyboard: view.kb };
  if (cq) {
    await tg(env, "editMessageText", {
      chat_id: chat,
      message_id: cq.message.message_id,
      text: view.text,
      reply_markup: markup,
    });
  } else {
    await tg(env, "sendMessage", { chat_id: chat, text: view.text, reply_markup: markup });
  }
}

async function dispatch(env, inputs) {
  const r = await fetch(
    `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${env.GH_WORKFLOW || "meetings.yml"}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "meet-menu-bot",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: env.GH_REF || "main", inputs }),
    }
  );
  return r.status; // 204 = запущено
}

async function handle(u, env) {
  const cq = u.callback_query;
  const chat = String((cq ? cq.message && cq.message.chat && cq.message.chat.id : u.message && u.message.chat && u.message.chat.id) ?? "");
  if (!chat || chat !== String(env.ALLOWED_CHAT_ID)) return;

  const schedule = JSON.parse(env.SCHEDULE_JSON);

  // --- обычное сообщение ---
  if (!cq) {
    const text = ((u.message && u.message.text) || "").trim();
    const link = text.match(/https:\/\/meet\.google\.com\/\S+/i);
    if (link) {
      const status = await dispatch(env, { force_url: link[0] });
      await tg(env, "sendMessage", {
        chat_id: chat,
        text: status === 204
          ? "🚀 Запускаю вход по ссылке (на 90 мин). Статус придёт в основной бот."
          : `❌ GitHub не принял запуск (${status})`,
      });
      return;
    }
    return show(env, chat, null, menuView(schedule));
  }

  // --- кнопки ---
  const [act, arg] = (cq.data || "").split("|");

  if (act === "m") {
    await tg(env, "answerCallbackQuery", { callback_query_id: cq.id });
    return show(env, chat, cq, menuView(schedule));
  }

  if (act === "d") {
    await tg(env, "answerCallbackQuery", { callback_query_id: cq.id });
    const day = arg === "today" ? todayKyiv() : arg;
    return show(env, chat, cq, dayView(schedule, day));
  }

  if (act === "j") {
    const idx = parseInt(arg, 10);
    const m = schedule[idx];
    if (!m) {
      await tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: "Такой пары нет" });
      return;
    }
    await tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: "Запускаю…" });
    const status = await dispatch(env, { force_index: String(idx) });
    const text = status === 204
      ? `🚀 Запустил: ${m.subject || "без названия"} (${m.start_time}–${m.end_time})\nЗаход через ~1 мин, статус придёт в основной бот.`
      : `❌ GitHub не принял запуск (${status}). Проверь GH_TOKEN, GH_REPO, GH_WORKFLOW.`;
    return show(env, chat, cq, { text, kb: [[{ text: "◀️ Меню", callback_data: "m" }]] });
  }
}
