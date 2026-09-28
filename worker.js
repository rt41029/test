// Cloudflare Worker — меню-бот Meet Bot.
// Управляет запусками GitHub Actions, тоглами пар (KV), авто-запуском по Киеву.
//
// Secrets / Vars:
//   MENU_BOT_TOKEN    токен ВТОРОГО бота (только меню)
//   ALLOWED_CHAT_ID   твой chat_id (тот же, что TELEGRAM_CHAT_ID)
//   WEBHOOK_SECRET    случайная строка (та же в setWebhook)
//   GH_TOKEN          fine-grained PAT: repo + Actions: Read and write
//   GH_REPO           owner/repo
//   SCHEDULE_JSON     то же содержимое, что в секрете SCHEDULE_JSON на GitHub
//   GH_REF            (опц.) main
//   GH_WORKFLOW       (опц.) meetings.yml
// Bindings:
//   SCHEDULE_KV       KV namespace (тоглы enabled)
// Cron:
//   * * * * *         авто-запуск пар (окно: за 8..6 минут до начала)

const DAYS = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"];
const DAY_RU  = { monday:"Понедельник",tuesday:"Вторник",wednesday:"Среда",thursday:"Четверг",friday:"Пятница",saturday:"Суббота",sunday:"Воскресенье" };
const DAY_SHORT = { monday:"Пн",tuesday:"Вт",wednesday:"Ср",thursday:"Чт",friday:"Пт",saturday:"Сб",sunday:"Вс" };
const KYIV_TZ = "Europe/Kyiv";

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("ok");
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    let update; try { update = await request.json(); } catch { return new Response("ok"); }
    try { await handle(update, env); }
    catch (e) { console.log("error", e && e.message); }
    return new Response("ok");
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(autoDispatch(env));
  },
};

const tg = (env, method, body) =>
  fetch(`https://api.telegram.org/bot${env.MENU_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(r => r.json());

// ---------- время по Киеву ----------
function kyivNow() {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: KYIV_TZ, weekday: "long",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(new Date());
  const o = {}; for (const p of parts) o[p.type] = p.value;
  return { weekday: String(o.weekday || "").toLowerCase(), hh: +o.hour, mm: +o.minute };
}
const curMin = (n) => n.hh * 60 + n.mm;

// ---------- расписание + тоглы (KV) ----------
async function loadSchedule(env) {
  const base = JSON.parse(env.SCHEDULE_JSON);
  let dis = [];
  try { const raw = await env.SCHEDULE_KV.get("disabled"); if (raw) dis = JSON.parse(raw); } catch {}
  return base.map((m, i) => ({ ...m, _i: i, enabled: m.enabled !== false && !dis.includes(i) }));
}
async function setEnabled(env, i, on) {
  let dis = [];
  try { const raw = await env.SCHEDULE_KV.get("disabled"); if (raw) dis = JSON.parse(raw); } catch {}
  dis = dis.filter(x => x !== i);
  if (!on) dis.push(i);
  await env.SCHEDULE_KV.put("disabled", JSON.stringify(dis));
}

// ---------- выбор «текущей» пары ----------
function pickDue(schedule, now) {
  const today = now.weekday, cm = curMin(now);
  let best = -1, bestScore = Infinity;
  for (const m of schedule) {
    if (!m.enabled) continue;
    if (String(m.day).toLowerCase() !== today) continue;
    const [sh, sm] = m.start_time.split(":").map(Number);
    const [eh, em] = m.end_time.split(":").map(Number);
    const s = sh * 60 + sm, e = eh * 60 + em;
    if (cm >= s - 20 && cm <= e) {
      const score = Math.abs(cm - s);
      if (score < bestScore) { bestScore = score; best = m._i; }
    }
  }
  return best;
}

// ---------- GitHub ----------
const ghHeaders = (env) => ({
  Authorization: `Bearer ${env.GH_TOKEN}`,
  Accept: "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28",
  "User-Agent": "meet-menu-bot",
  "Content-Type": "application/json",
});
const wfPath = (env) =>
  `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${env.GH_WORKFLOW || "meetings.yml"}`;

async function dispatch(env, inputs) {
  const r = await fetch(`${wfPath(env)}/dispatches`, {
    method: "POST", headers: ghHeaders(env),
    body: JSON.stringify({ ref: env.GH_REF || "main", inputs }),
  });
  return r.status; // 204 = запущено
}

async function activeRun(env) {
  const r = await fetch(`${wfPath(env)}/runs?status=in_progress&per_page=5`, { headers: ghHeaders(env) });
  if (!r.ok) return null;
  const j = await r.json();
  const list = (j.workflow_runs || []).filter(x => x.status === "in_progress" || x.status === "queued");
  return list[0] || null;
}

async function cancelRun(env, id) {
  const r = await fetch(`https://api.github.com/repos/${env.GH_REPO}/actions/runs/${id}/cancel`, {
    method: "POST", headers: ghHeaders(env),
  });
  return r.status; // 202 = ок
}

// ---------- виды ----------
function menuView() {
  return {
    text: "🤖 <b>Meet Bot</b>\nВсё управление — кнопками. Ссылку на Meet можно просто прислать сообщением.",
    kb: [
      [{ text: "🚀 Зайти на текущую", callback_data: "a|join" }],
      [{ text: "🔄 Перезапустить", callback_data: "a|restart" },
       { text: "⏹️ Остановить", callback_data: "a|stop" }],
      [{ text: "📊 Статус", callback_data: "a|status" },
       { text: "📅 Расписание", callback_data: "a|sched" }],
    ],
  };
}

function schedView(schedule) {
  const rows = schedule.map(m => {
    const d = DAY_SHORT[m.day] || m.day;
    const mark = m.enabled ? "🟢" : "⚪";
    return [{
      text: `${mark} ${d} ${m.start_time}–${m.end_time} ${(m.subject || "").slice(0, 24)}`,
      callback_data: `t|${m._i}`,
    }];
  });
  rows.push([{ text: "◀️ Меню", callback_data: "m" }]);
  return { text: "📅 Тапни пару, чтобы включить/выключить автозапуск.", kb: rows };
}

async function statusView(env) {
  const run = await activeRun(env);
  if (!run) {
    return { text: "🔴 Сейчас ничего не запущено.", kb: [[{ text: "◀️ Меню", callback_data: "m" }]] };
  }
  const started = new Date(run.created_at);
  const mins = Math.floor((Date.now() - started.getTime()) / 60000);
  return {
    text: `🟢 <b>Активный запуск</b> #${run.run_number}\nНачат: ${started.toISOString().slice(11,16)} UTC (${mins} мин назад)\nСтатус: ${run.status}\n<a href="${run.html_url}">открыть в GitHub</a>`,
    kb: [
      [{ text: "🔄 Перезапустить", callback_data: "a|restart" },
       { text: "⏹️ Остановить", callback_data: "a|stop" }],
      [{ text: "◀️ Меню", callback_data: "m" }],
    ],
  };
}

async function show(env, chat, cq, view) {
  const reply_markup = { inline_keyboard: view.kb };
  const base = { chat_id: chat, text: view.text, parse_mode: "HTML",
                 disable_web_page_preview: true, reply_markup };
  if (cq) {
    await tg(env, "editMessageText", { ...base, message_id: cq.message.message_id });
  } else {
    await tg(env, "sendMessage", base);
  }
}

// ---------- обработчик ----------
async function handle(u, env) {
  const cq = u.callback_query;
  const chat = String((cq ? cq.message?.chat?.id : u.message?.chat?.id) ?? "");
  if (!chat || chat !== String(env.ALLOWED_CHAT_ID)) return;

  // обычное сообщение: ссылка на Meet -> запуск, иначе меню
  if (!cq) {
    const text = (u.message?.text || "").trim();
    const link = text.match(/https:\/\/meet\.google\.com\/\S+/i);
    if (link) {
      const st = await dispatch(env, { force_url: link[0] });
      await tg(env, "sendMessage", {
        chat_id: chat,
        text: st === 204 ? "🚀 Запускаю вход по ссылке (90 мин). Статус и скрины — в основном боте."
                         : `❌ GitHub не принял запуск (${st}). Проверь GH_TOKEN/GH_REPO.`,
      });
      return;
    }
    return show(env, chat, null, menuView());
  }

  const [act, arg] = (cq.data || "").split("|");

  if (act === "m") {
    await tg(env, "answerCallbackQuery", { callback_query_id: cq.id });
    return show(env, chat, cq, menuView());
  }

  if (act === "a" && arg === "sched") {
    await tg(env, "answerCallbackQuery", { callback_query_id: cq.id });
    return show(env, chat, cq, schedView(await loadSchedule(env)));
  }

  if (act === "t") {
    const i = parseInt(arg, 10);
    const sch = await loadSchedule(env);
    const cur = sch.find(m => m._i === i);
    if (!cur) {
      await tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: "Нет такой пары" });
      return;
    }
    await setEnabled(env, i, !cur.enabled);
    await tg(env, "answerCallbackQuery", {
      callback_query_id: cq.id, text: !cur.enabled ? "🟢 Включено" : "⚪ Выключено",
    });
    return show(env, chat, cq, schedView(await loadSchedule(env)));
  }

  if (act === "a") {
    if (arg === "status") {
      await tg(env, "answerCallbackQuery", { callback_query_id: cq.id });
      return show(env, chat, cq, await statusView(env));
    }

    if (arg === "join" || arg === "restart") {
      if (arg === "restart") {
        const run = await activeRun(env);
        if (run) {
          await cancelRun(env, run.id);
          await new Promise(r => setTimeout(r, 3000));
        }
      }
      const now = kyivNow();
      const sch = await loadSchedule(env);
      const idx = pickDue(sch, now);
      if (idx < 0) {
        await tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: "Сейчас пар нет" });
        return;
      }
      const m = sch.find(x => x._i === idx);
      await tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: "Запускаю…" });
      const st = await dispatch(env, { force_index: String(idx) });
      return show(env, chat, cq, {
        text: st === 204
          ? `${arg === "restart" ? "🔄" : "🚀"} Запустил: <b>${m.subject || "без названия"}</b> (${m.start_time}–${m.end_time})\nСкрины и кнопки — в основном боте.`
          : `❌ GitHub вернул ${st}. Проверь GH_TOKEN / GH_REPO / GH_WORKFLOW.`,
        kb: [[{ text: "◀️ Меню", callback_data: "m" }]],
      });
    }

    if (arg === "stop") {
      const run = await activeRun(env);
      if (!run) {
        await tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: "Нечего останавливать" });
        return;
      }
      const st = await cancelRun(env, run.id);
      await tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: st === 202 ? "Отменяю…" : `Ошибка ${st}` });
      return show(env, chat, cq, {
        text: st === 202 ? `⏹️ Отменил запуск #${run.run_number}.` : `❌ GitHub вернул ${st}.`,
        kb: [[{ text: "◀️ Меню", callback_data: "m" }]],
      });
    }
  }

  await tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: "?" });
}

// ---------- авто-запуск по cron (окно: за 8..6 минут до начала пары) ----------
async function autoDispatch(env) {
  try {
    const now = kyivNow();
    const sch = await loadSchedule(env);
    const cm = curMin(now);

    for (const m of sch) {
      if (!m.enabled) continue;
      if (String(m.day).toLowerCase() !== now.weekday) continue;
      const [sh, sm] = m.start_time.split(":").map(Number);
      const lead = sh * 60 + sm - cm;
      if (lead > 8 || lead < 6) continue;

      if (await activeRun(env)) return; // уже что-то идёт

      const st = await dispatch(env, { force_index: String(m._i) });
      if (st === 204) {
        await tg(env, "sendMessage", {
          chat_id: env.ALLOWED_CHAT_ID,
          text: `⏰ Автозапуск: <b>${m.subject || "без названия"}</b> (${m.start_time})`,
        });
      }
      return;
    }
  } catch (e) {
    console.log("autoDispatch error", e && e.message);
  }
}
