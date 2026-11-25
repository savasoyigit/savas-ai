// K-3 · SAVAS AI sanal ofis. Every value on screen comes from the orchestrator
// API (docs/plan-k.md, karar D); a missing value says so. K-4: actions call the
// orchestrator's own endpoints, each after an in-page confirmation (karar G).
import {
  ROOMS, STATUS_LABEL, TASK_STATUS_LABEL, FINAL, agentState, recentFailures, handoffs,
  eventKind, eventText, eventAgent, isNoise, roleLabel, clip,
} from "./state.js";
import { character, characterSvg, furnitureSvg } from "./sprites.js";

const POLL_MS = 3000;
const DETAIL_TASKS = 5;
const OBSIDIAN_ACTIVE_MS = 60 * 1000;

const state = {
  health: null, workers: [], tasks: [], activity: [], usage: null, approvals: [],
  metrics: null, metricsDays: 7, // L-1a
  goals: null, // GET /goals (L-3b); null until read
  details: {}, connected: false, polled: false, lastOk: null, error: null, partial: [],
  reviewing: {}, // J-6: task id -> when its manual review started (its detail is read again until it ends)
  selected: null, // {type: "agent"|"task"|"job", id}
  jobs: null, // GET /jobs (J-3): {counts, jobs}; null until read
  jobTab: "new", // new | filtered
  jobDetails: {}, // job_id -> GET /jobs/{id} (with the description)
  showAll: false,
  session: null, // the gateway user; null on local access (no login, no push)
  sessionChecked: false,
  flagged: null, // flagged memory facts; null = not readable (the gateway does not pass /memory, A5)
  activeFacts: [],
  confirm: null, // key of the action waiting for "Evet"
  busy: null, // key of the action running
  notice: null, // {key, text, error} of the last action; shown next to that action
  agentTab: "genel", // genel | sohbet | aktivite | performans (K-5)
  chat: {}, // worker -> {conversation_id, turns, fresh, sending, error}
  push: { enabled: false, subscribed: false, permission: "default", busy: false, message: "" },
};

const $ = (id) => document.getElementById(id);
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)");

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "style") el.style.cssText = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const time = (iso) => (iso ? new Date(iso).toLocaleTimeString("tr-TR") : "–");
const dateTime = (iso) => (iso ? new Date(iso).toLocaleString("tr-TR") : "–");
const num = (n) => (n == null ? "bilinmiyor" : Number(n).toLocaleString("tr-TR"));

function duration(task) {
  const start = Date.parse(task.created_at);
  const end = Date.parse(task.completed_at || "");
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null;
  const s = Math.round((end - start) / 1000);
  return s < 60 ? `${s} sn` : `${Math.floor(s / 60)} dk ${s % 60} sn`;
}

// ------------------------------------------------------------------
// data
// ------------------------------------------------------------------

async function api(path) {
  const response = await fetch(path, { cache: "no-store", credentials: "same-origin" });
  // Through the gateway (K-3b) an expired session sends the page to the login.
  if (response.status === 401) {
    location.href = `/login?next=${encodeURIComponent(location.pathname)}`;
    throw new Error("oturum gerekli");
  }
  if (!response.ok) throw new Error(`${path} → HTTP ${response.status}`);
  return response.json();
}

async function poll() {
  // /health decides "connected". Another endpoint failing keeps its last good
  // value and is named in the banner (K-6): one broken read does not blank the page.
  const reads = [
    ["/health", (d) => { state.health = d; }],
    ["/workers", (d) => { state.workers = d.workers || []; }],
    ["/tasks?limit=15", (d) => { state.tasks = d.tasks || []; }],
    ["/activity?limit=80", (d) => { state.activity = d.events || []; }],
    ["/usage/today", (d) => { state.usage = d; }],
    ["/approvals?status=pending", (d) => { state.approvals = d.approvals || []; }],
    ["/jobs?limit=200", (d) => { state.jobs = d; }],
    ["/goals", (d) => { state.goals = d.goals || []; }],
  ];
  const results = await Promise.allSettled(reads.map(([path]) => api(path)));
  const failed = [];
  results.forEach((result, i) => {
    if (result.status === "fulfilled") reads[i][1](result.value);
    else failed.push(reads[i][0].split("?")[0]);
  });
  if (results[0].status === "fulfilled") {
    Object.assign(state, { connected: true, lastOk: new Date(), error: null, partial: failed });
    // A package being written: its panel follows it.
    const sel = state.selected;
    const writing = sel?.type === "job" && state.jobDetails[sel.id]?.package?.status === "running";
    await Promise.all([loadDetails(), loadMemory(), pollChat(), writing ? loadJob(sel.id) : null]);
  } else {
    Object.assign(state, { connected: false, error: results[0].reason?.message || "bağlantı hatası", partial: [] });
  }
  state.polled = true;
  render();
}

// Details of the newest tasks (children, persisted events). Final ones never change: cached.
async function loadDetails() {
  const ids = state.tasks.slice(0, DETAIL_TASKS).map((t) => t.task_id);
  if (state.selected?.type === "task" && !ids.includes(state.selected.id)) ids.push(state.selected.id);
  await Promise.all(ids.map(async (id) => {
    const cached = state.details[id];
    const since = state.reviewing[id];
    if (cached && FINAL.has(cached.task?.status) && !since) return;
    try {
      const detail = state.details[id] = await api(`/tasks/${encodeURIComponent(id)}`);
      // Its "started" event is written before the button gets its answer: done when a finish follows it.
      const last = (detail.activity || []).filter((e) => e.event === "review" && e.manual).at(-1);
      if (since && last && last.status !== "started") delete state.reviewing[id];
    } catch {
      /* the list still shows the task; its detail says it could not be read */
    }
  }));
}

// Flagged facts (Faz F) for the user's choice. Local access only: the gateway
// does not pass /memory (plan-k A5), so the page does not ask there.
async function loadMemory() {
  if (!state.sessionChecked || state.session) {
    state.flagged = null;
    return;
  }
  try {
    const flagged = (await api("/memory/facts?status=flagged&limit=50")).facts || [];
    state.activeFacts = flagged.length ? (await api("/memory/facts?status=active&limit=500")).facts || [] : [];
    state.flagged = flagged;
  } catch {
    state.flagged = null;
  }
}

function obsidianAgent() {
  const waiting = state.approvals.some((a) => a.agent === "obsidian");
  const last = [...state.activity].reverse().find((e) => e.agent === "obsidian");
  const lastAt = last ? Date.parse(last.timestamp) : null;
  let status = "unknown";
  let reason = "Sağlık bilgisi orchestrator'dan alınmıyor; yalnız işlemleri görünür.";
  if (!state.connected) reason = "Orchestrator'a ulaşılamıyor.";
  else if (waiting) { status = "waiting"; reason = "Not işlemi kullanıcı onayı bekliyor."; }
  else if (lastAt && Date.now() - lastAt < OBSIDIAN_ACTIVE_MS) { status = "active"; reason = `Not işlemi: ${last.event} · ${last.status}`; }
  return {
    worker: { id: "obsidian", name: "Obsidian", role: "archive", model: null, tasks: [] },
    view: { status, room: "arch", task: null, reason, lastAt },
  };
}

function agentViews() {
  const failures = recentFailures(state.tasks, state.details);
  const ctx = { health: state.health, workers: state.workers, failures, connected: state.connected };
  const views = state.workers.map((worker) => ({ worker, view: agentState(worker, ctx) }));
  views.push(obsidianAgent());
  return views;
}

// ------------------------------------------------------------------
// office
// ------------------------------------------------------------------

const DECOR = {
  obs: [["telescope", 6, 8, 56], ["shelf", 66, 6, 70], ["plant", 84, 56, 34]],
  hq: [["board", 26, 4, 120], ["table", 30, 46, 110], ["plant", 4, 70, 34], ["plant", 86, 70, 34]],
  scan: [["rack", 4, 6, 40], ["rack", 18, 6, 40], ["desk", 62, 8, 76]],
  lab: [["desk", 4, 8, 76], ["desk", 52, 8, 76], ["rack", 88, 30, 34]],
  studio: [["easel", 6, 6, 60], ["desk", 56, 8, 76], ["plant", 86, 58, 30]],
  arch: [["shelf", 4, 6, 70], ["shelf", 36, 6, 70], ["cabinet", 80, 8, 40]],
  rest: [["sofa", 4, 34, 110], ["cooler", 44, 10, 30], ["table", 60, 50, 80], ["plant", 90, 8, 34]],
};

const roomEls = {};
const agentEls = {};

function buildOffice() {
  const office = $("office");
  for (const room of ROOMS) {
    const decor = h("div", { class: "decor" });
    for (const [name, left, top, width] of DECOR[room.id] || []) {
      const svg = furnitureSvg(name);
      svg.style.cssText = `left:${left}%;top:${top}%;width:${width}px`;
      decor.append(svg);
    }
    const count = h("small", {}, "");
    const el = h("div", { class: "room", "data-room": room.id, style: `grid-area:${room.id}` },
      h("div", { class: "room-plate" }, h("span", {}, room.name), count),
      decor,
      h("div", { class: "agents" }));
    el.title = room.purpose;
    roomEls[room.id] = { el, count, zone: el.querySelector(".agents") };
    office.append(el);
  }
  $("logo-mark").append(characterSvg("ares"));
}

function agentEl(worker) {
  if (agentEls[worker.id]) return agentEls[worker.id];
  const c = character(worker.id);
  const el = h("button", {
    type: "button", class: "agent", style: `--agent:${c.color}`,
    onclick: () => select({ type: "agent", id: worker.id }),
  },
  h("span", { class: "bubble" }),
  h("span", { class: "avatar" }, characterSvg(worker.id)),
  h("span", { class: "tag" }, worker.name),
  h("span", { class: "state" }));
  agentEls[worker.id] = el;
  return el;
}

// Move to the room the data puts the agent in; the walk is animated (FLIP).
function place(el, zone) {
  if (el.parentElement === zone) return;
  const first = el.isConnected ? el.getBoundingClientRect() : null;
  zone.append(el);
  if (!first || reduceMotion.matches) return;
  const last = el.getBoundingClientRect();
  el.animate(
    [{ transform: `translate(${first.left - last.left}px, ${first.top - last.top}px)` }, { transform: "none" }],
    { duration: 900, easing: "ease-in-out" },
  );
}

const BUBBLE = { thinking: "…", waiting: "⏸", error: "!", unknown: "?" };

function renderOffice(views) {
  const perRoom = {};
  for (const { worker, view } of views) {
    const el = agentEl(worker);
    el.dataset.status = view.status;
    el.classList.toggle("selected", state.selected?.type === "agent" && state.selected.id === worker.id);
    el.querySelector(".state").textContent = STATUS_LABEL[view.status];
    el.querySelector(".bubble").textContent = BUBBLE[view.status] || "";
    el.title = `${worker.name} — ${STATUS_LABEL[view.status]}: ${view.reason}`;
    el.setAttribute("aria-label", el.title);
    place(el, roomEls[view.room].zone);
    perRoom[view.room] = perRoom[view.room] || { total: 0, busy: 0 };
    perRoom[view.room].total += 1;
    if (["active", "thinking", "waiting"].includes(view.status)) perRoom[view.room].busy += 1;
  }
  for (const room of ROOMS) {
    const info = perRoom[room.id];
    const { el, count } = roomEls[room.id];
    el.classList.toggle("busy", Boolean(info?.busy));
    count.textContent = !info ? "boş" : info.busy ? `${info.busy} çalışıyor` : `${info.total} ajan`;
  }
  requestAnimationFrame(() => renderLinks());
}

function renderLinks() {
  const svg = $("links");
  svg.replaceChildren();
  if (!state.connected) return;
  const box = $("office").getBoundingClientRect();
  for (const link of handoffs(state.workers)) {
    const a = agentEls[link.from]?.querySelector(".avatar")?.getBoundingClientRect();
    const b = agentEls[link.to]?.querySelector(".avatar")?.getBoundingClientRect();
    if (!a || !b) continue;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", a.left + a.width / 2 - box.left);
    line.setAttribute("y1", a.top + a.height / 2 - box.top);
    line.setAttribute("x2", b.left + b.width / 2 - box.left);
    line.setAttribute("y2", b.top + b.height / 2 - box.top);
    const color = character(link.to).color;
    line.setAttribute("stroke", color);
    line.style.color = color;
    svg.append(line);
  }
}

// ------------------------------------------------------------------
// hud, energy, feed, tasks
// ------------------------------------------------------------------

function stat(label, value, cls = "") {
  return h("div", { class: `stat ${cls}` }, h("span", {}, label), h("b", {}, value));
}

function renderHud(views) {
  const hud = $("hud");
  if (!state.health) {
    hud.replaceChildren(stat("Durum", "veri yok", "bad"));
    return;
  }
  const workers = views.filter((v) => v.worker.id !== "obsidian");
  const busy = workers.filter((v) => ["active", "thinking", "waiting"].includes(v.view.status)).length;
  const idle = workers.filter((v) => v.view.status === "idle").length;
  const offline = workers.filter((v) => v.view.status === "offline").length;
  const running = state.tasks.filter((t) => t.status === "running" || t.status === "queued").length;
  const failedRecent = state.tasks.filter((t) => t.status === "failed").length;
  const local = state.health.local_llm || {};
  const localText = !local.configured ? "yok" : !local.reachable ? "ulaşılamıyor"
    : Object.values(local.models || {}).join(", ") || "hazır";
  const alerts = state.health.model_alerts?.count ?? 0;
  hud.replaceChildren(
    stat("Ajan", workers.length),
    stat("Çalışan", busy, busy ? "" : ""),
    stat("Boşta", idle),
    stat("Çevrimdışı", offline, offline ? "bad" : ""),
    stat("Aktif görev", running),
    stat("Onay bekleyen", state.approvals.length, state.approvals.length ? "warn" : ""),
    stat("Başarısız (son 15)", failedRecent, failedRecent ? "bad" : ""),
    stat("Model alarmı", alerts, alerts ? "warn" : ""),
    stat("Yerel LLM", localText, local.configured && !local.reachable ? "bad" : ""),
    stat("Ares modeli", state.health.routes?.ares || "–"),
  );
}

function renderEnergy(views) {
  const usage = state.usage;
  const fill = $("energy-fill");
  if (!usage) {
    $("energy-num").textContent = "bilinmiyor";
    fill.style.width = "0";
    $("energy-note").textContent = "Kullanım verisi alınamadı.";
  } else {
    const { used, budget, remaining, local_tokens: local } = usage.system;
    const ratio = budget > 0 ? remaining / budget : 0;
    $("energy-num").textContent = `${num(remaining)} / ${num(budget)}`;
    fill.style.width = `${Math.max(0, Math.min(1, ratio)) * 100}%`;
    fill.className = `energy-fill${ratio <= 0 ? " empty" : ratio < 0.25 ? " low" : ""}`;
    $("energy-note").textContent =
      `${usage.date} · kullanılan ${num(used)} bulut token · yerel ${num(local)} (bütçeye sayılmaz)` +
      (ratio <= 0 ? " · bütçe doldu: çağrılar yerel modele gider" : "");
  }
  const workers = views.filter((v) => v.worker.id !== "obsidian");
  const busy = workers.filter((v) => ["active", "thinking", "waiting"].includes(v.view.status)).length;
  $("working-num").textContent = state.connected ? `${busy} / ${workers.length}` : "– / –";
}

function feedEvents() {
  if (state.activity.length) return { events: state.activity, source: "" };
  const latest = state.tasks[0] && state.details[state.tasks[0].task_id];
  if (latest?.activity?.length) {
    return { events: latest.activity, source: "Canlı kayıt boş (orchestrator yeniden başladı); son görevin kalıcı kaydı gösteriliyor." };
  }
  return { events: [], source: "" };
}

function renderFeed() {
  const { events, source } = feedEvents();
  $("feed-source").textContent = source;
  const shown = events.filter((e) => state.showAll || !isNoise(e)).slice(-60).reverse();
  const list = $("feed-list");
  if (!state.connected && !events.length) {
    list.replaceChildren(h("li", { class: "k-error" }, h("time", {}, "–"), h("span", { class: "who" }, "sistem"), h("span", {}, "Bağlantı yok.")));
    return;
  }
  if (!shown.length) {
    list.replaceChildren(h("li", { class: "k-info" }, h("time", {}, "–"), h("span", { class: "who" }, "–"), h("span", {}, "Henüz olay yok.")));
    return;
  }
  list.replaceChildren(...shown.map((e) => {
    const item = h("li", { class: `k-${eventKind(e)}`, title: JSON.stringify(e) },
      h("time", {}, time(e.timestamp)), h("span", { class: "who" }, eventAgent(e)), h("span", {}, eventText(e)));
    if (e.task_id) {
      item.style.cursor = "pointer";
      item.addEventListener("click", () => select({ type: "task", id: e.task_id }));
    }
    return item;
  }));
}

function chip(status) {
  return h("span", { class: `chip st-${status}` }, TASK_STATUS_LABEL[status] || status);
}

function renderTasks() {
  const list = $("task-list");
  if (!state.tasks.length) {
    list.replaceChildren(h("li", { class: "muted" }, state.connected ? "Görev yok." : "Bağlantı yok."));
    return;
  }
  list.replaceChildren(...state.tasks.map((task) => h("li", {},
    h("button", { type: "button", onclick: () => select({ type: "task", id: task.task_id }) },
      chip(task.status), h("time", {}, `${dateTime(task.created_at)}${duration(task) ? ` · ${duration(task)}` : ""}`),
      h("span", { class: "msg" }, clip(task.message, 140))))));
}

// ------------------------------------------------------------------
// job listings (J-3): read only; applying stays with the user (plan-j H)
// ------------------------------------------------------------------

const JOB_TABS = [["new", "Yeni"], ["track", "Takip"], ["filtered", "Elenen"]];
// J-5: the "Takip" tab holds what the user has acted on.
const TRACKED = new Set(["package_ready", "applied", "interview", "rejected", "offer"]);
const tabOf = (status) => (TRACKED.has(status) ? "track" : status);
const JOB_STATUS_NAMES = { new: "yeni", filtered: "elendi", dismissed: "gizlendi", package_ready: "paket hazır",
  applied: "başvuruldu", interview: "mülakat", rejected: "red", offer: "teklif" };
const SOURCE_NAMES = { linkedin: "LinkedIn", kariyer: "Kariyer.net", remotive: "Remotive", mail: "E-posta" };

function renderJobs() {
  const counts = state.jobs?.counts || {};
  $("jobs-tabs").replaceChildren(...JOB_TABS.map(([key, label]) => h("button", {
    type: "button", role: "tab", class: `tab${state.jobTab === key ? " active" : ""}`, "aria-selected": String(state.jobTab === key),
    onclick: () => { state.jobTab = key; renderJobs(); },
  }, `${label} (${key === "track" ? [...TRACKED].reduce((n, s) => n + (counts[s] || 0), 0) : counts[key] || 0})`)));

  const list = $("job-list");
  // New ones best first (J-4 score); unscored after the scored ones.
  const jobs = (state.jobs?.jobs || []).filter((job) => tabOf(job.status) === state.jobTab)
    .sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
  if (!jobs.length) {
    const empty = !state.jobs ? (state.connected ? "Okunuyor…" : "Bağlantı yok.")
      : state.jobTab === "new" ? "Yeni ilan yok. Toplama 08:30 ve 18:30'da çalışır."
      : state.jobTab === "track" ? "Paket hazırlanan ya da başvurulan ilan yok." : "Elenen ilan yok.";
    list.replaceChildren(h("li", { class: "muted" }, empty));
    return;
  }
  list.replaceChildren(...jobs.map((job) => h("li", {},
    h("button", { type: "button", onclick: () => select({ type: "job", id: job.job_id }) },
      h("span", { class: "chip" }, SOURCE_NAMES[job.source] || job.source),
      h("time", {}, `${dateTime(job.found_at)}${job.score != null ? " · " : ""}`,
        job.score != null ? h("b", { class: `score ${scoreClass(job.score)}` }, `${job.score} puan`) : null),
      h("span", { class: "msg" }, clip(job.title, 120)),
      h("span", { class: "co" }, [state.jobTab === "track" ? JOB_STATUS_NAMES[job.status] : null,
        job.company, job.location, job.remote ? "remote" : null, job.filter_reason ? `✕ ${job.filter_reason}` : null]
        .filter(Boolean).join(" · "))))));
}

// ------------------------------------------------------------------
// L-3b Hedefler (plan-l3): the user starts a goal; a recipe in code writes
// packages for the best listings. Nothing is sent anywhere.
// ------------------------------------------------------------------

const GOAL_PACKAGES = 3;
const GOAL_STATUS = { running: "Çalışıyor…", waiting: "İlan metni bekliyor", completed: "Bitti", stopped: "Durdu", failed: "Başarısız" };
const GOAL_STEP = { pending: "sırada", needs_text: "ilan metni gerekli", running: "yazılıyor…", ready: "hazır",
  needs_fix: "düzeltilmeli", failed: "başarısız", skipped: "atlandı" };

async function continueGoal(goal) {
  const texts = {};
  for (const step of goal.steps) {
    const text = (pasteInputs[step.job_id]?.value || "").trim();
    if (step.status === "needs_text" && text) texts[step.job_id] = text;
  }
  await act(`/goals/${encodeURIComponent(goal.goal_id)}/continue`, { texts });
  for (const id of Object.keys(texts)) pasteInputs[id].value = "";
  return "Devam ediyor.";
}

function goalCard(goal) {
  const open = goal.status === "waiting" || goal.status === "stopped";
  return h("li", { class: "goal" },
    h("div", { class: "box-head" },
      h("b", {}, `İş arama haftası · ${GOAL_STATUS[goal.status] || goal.status}`),
      h("span", { class: "muted small" }, `${dateTime(goal.created_at)} · ${num(goal.used)} / ${num(goal.budget)} token`)),
    goal.error ? h("p", { class: "small" }, `Durdu: ${goal.error}`) : null,
    h("ol", { class: "goal-steps" }, goal.steps.map((step) => h("li", {},
      h("span", {}, `${step.title} · ${step.company || "-"} (puan ${step.score}): `),
      h("b", {}, GOAL_STEP[step.status] || step.status),
      step.tokens ? h("span", { class: "muted small" }, ` · ${num(step.tokens)} token`) : null,
      step.error ? h("span", { class: "muted small" }, ` · ${step.error}`) : null,
      step.url ? h("a", { href: step.url, target: "_blank", rel: "noopener noreferrer", class: "small" }, " ilan ↗") : null,
      step.status === "needs_text" && goal.status === "waiting" ? pasteInput(step.job_id) : null))),
    open ? confirmable(`goal-continue-${goal.goal_id}`, "Devam",
      "Yapıştırılan metinlerle / kalan ilanlarla devam edilsin mi?", () => continueGoal(goal)) : null,
    state.notice?.key === `goal-continue-${goal.goal_id}`
      ? h("p", { class: `small${state.notice.error ? " error" : ""}` }, state.notice.text) : null);
}

function renderGoals() {
  const box = $("goals-body");
  if (!box) return;
  const goals = state.goals;
  const running = (goals || []).some((g) => g.status === "running");
  const start = confirmable("goal-start", "Başlat",
    `Puanı en yüksek ${GOAL_PACKAGES} ilan için paket hazırlansın mı? (en çok 120.000 token, yalnız bulut)`,
    async () => { await act("/goals", { kind: "job_week", packages: GOAL_PACKAGES }); return "Başladı."; },
    { disabled: running });
  box.replaceChildren(
    h("div", { class: "goal-actions" }, h("span", { class: "small" }, "İş arama haftası: "), start,
      state.notice?.key === "goal-start" ? h("span", { class: `small${state.notice.error ? " error" : ""}` }, ` ${state.notice.text}`) : null),
    goals == null ? h("p", { class: "muted" }, "Okunuyor…")
      : goals.length ? h("ol", { class: "goal-list" }, goals.map(goalCard)) : h("p", { class: "muted" }, "Henüz hedef yok."));
}

// ------------------------------------------------------------------
// L-1a Metrikler (GET /metrics; read on its own, not on every poll)
// ------------------------------------------------------------------

const METRIC_DAYS = [1, 7, 30];
const METRICS_MS = 60000;
const pct = (n) => (n == null ? "–" : `%${n}`);
const few = (s) => (s?.few ? h("span", { class: "muted small" }, ` (az veri, n=${s.n})`) : s ? h("span", { class: "muted small" }, ` (n=${s.n})`) : null);

async function loadMetrics() {
  const days = state.metricsDays;
  try {
    const data = await api(`/metrics?days=${days}`);
    if (days === state.metricsDays) state.metrics = data;
  } catch (error) {
    state.metrics = { error: error.message };
  }
  renderMetrics();
}

function table(head, rows) {
  return h("div", { class: "metric-scroll" }, h("table", { class: "metric-table" },
    h("thead", {}, h("tr", {}, head.map((x) => h("th", {}, x)))),
    h("tbody", {}, rows.length ? rows.map((r) => h("tr", {}, r.map((x) => h("td", {}, x))))
      : h("tr", {}, h("td", { colspan: head.length, class: "muted" }, "Kayıt yok.")))));
}

function renderMetrics() {
  $("metrics-tabs").replaceChildren(...METRIC_DAYS.map((d) => h("button", {
    type: "button", role: "tab", class: `tab${state.metricsDays === d ? " active" : ""}`, "aria-selected": String(state.metricsDays === d),
    onclick: () => { state.metricsDays = d; state.metrics = null; renderMetrics(); loadMetrics(); },
  }, d === 1 ? "Bugün" : `${d} gün`)));
  const m = state.metrics, body = $("metrics-body");
  if (!m || m.error) {
    body.replaceChildren(h("p", { class: "muted" }, m?.error ? `Okunamadı: ${m.error}` : "Okunuyor…"));
    return;
  }
  const t = m.tokens, e = m.errors, q = m.quality;
  const top = Math.max(1, ...t.by_day.map((d) => d.cloud + d.local));
  const bars = h("div", { class: "metric-days" }, t.by_day.map((d) => h("div", { class: "metric-day", title: `${d.day}: bulut ${num(d.cloud)} · yerel ${num(d.local)}` },
    h("div", { class: "metric-bar" },
      h("div", { class: "cloud", style: `height:${(d.cloud * 100) / top}%` }),
      h("div", { class: "local", style: `height:${(d.local * 100) / top}%` })),
    m.days <= 7 ? h("span", {}, d.day.slice(5)) : null)));
  const kinds = Object.entries(t.per_kind).map(([k, v]) => `${k}: ${v.tasks} görev, ort. ${num(v.average)} / medyan ${num(v.median)}`);
  body.replaceChildren(
    h("h3", {}, "Token"),
    h("p", {}, `${m.from} – ${m.to} · bulut ${num(t.cloud)} · yerel ${num(t.local)} · ${num(t.calls)} çağrı`,
      t.unlinked_calls ? h("span", { class: "muted small" }, ` · görevle bağlanamayan çağrı: ${t.unlinked_calls}`) : null),
    bars,
    h("p", { class: "muted small" }, "■ bulut  ", h("span", { class: "metric-local-key" }, "■ yerel"), kinds.length ? ` · ${kinds.join(" · ")}` : ""),
    table(["Ajan", "Model", "Yer", "Çağrı", "Token"], t.by_agent_model.map((r) => [r.agent, r.model, r.location === "local" ? "yerel" : "bulut", num(r.calls), num(r.tokens)])),
    h("h4", {}, "En pahalı görevler"),
    table(["Görev", "Tür", "Durum", "Token"], t.top_tasks.map((r) => [
      h("button", { type: "button", class: "link-btn", onclick: () => select({ type: "task", id: r.task_id }) }, clip(r.message || r.task_id, 70)),
      r.kind, TASK_STATUS_LABEL[r.status] || r.status, num(r.tokens)])),
    h("h3", {}, "Hata"),
    h("p", {}, `Yedek modele düşme: ${e.fallbacks} · bütçe reddi: ${e.budget_refusals} · bağlam bütçesi aşımı: ${e.context_over_budget}`),
    table(["Model", "Çağrı", "Başarısız", "Oran"], Object.entries(e.calls_by_model).map(([k, v]) => [k, num(v.calls), num(v.failed), pct(v.failed_percent)])),
    table(["Görev türü", "Toplam", "Tamam", "İncelenmeli", "Başarısız", "Engellendi", "İptal"],
      Object.entries(e.tasks_by_kind).map(([k, v]) => [k, v.total, v.completed, v.needs_review, v.failed, v.blocked, v.cancelled])),
    h("h3", {}, "Model başarısı"),
    table(["Model", "Çağrı", "Başarılı", "Ort. süre", "Token/çağrı", "Gordon onayı", "Kod görevi", "Doğrulanamayan iddia"],
      q.models.map((r) => [r.model, h("span", {}, num(r.calls), few(r.sample)), pct(r.ok_percent),
        r.average_ms == null ? "–" : `${(r.average_ms / 1000).toFixed(1)} sn`, num(r.tokens_per_call),
        h("span", {}, pct(r.reviews.approve_percent), few(r.reviews)), h("span", {}, pct(r.code.ok_percent), few(r.code)),
        r.unsupported_claims])),
    h("p", {}, "Başvuru paketi düzeltme oranı: ", pct(q.packages.needs_fix_percent), few(q.packages),
      " · onay kartlarında red: ", pct(q.approvals.rejected_percent), few(q.approvals),
      q.unlinked_signals ? h("span", { class: "muted small" }, ` · modele bağlanamayan sinyal: ${q.unlinked_signals}`) : null),
    h("p", { class: "muted small" }, m.note));
}

const TRACK_NAMES = { dev: "Geliştirici", pm_ba: "Proje yönetimi / iş analizi" };
const scoreClass = (score) => (score >= 70 ? "good" : score >= 40 ? "mid" : "low");

async function loadJob(id) {
  try {
    state.jobDetails[id] = await api(`/jobs/${encodeURIComponent(id)}`);
  } catch (error) {
    state.jobDetails[id] = { error: error.message };
  }
}

function jobPanel(id) {
  const job = state.jobDetails[id] || (state.jobs?.jobs || []).find((j) => j.job_id === id);
  if (!job) return [h("div", { class: "muted" }, "İlan bulunamadı.")];
  if (job.error) return [h("div", { class: "muted" }, `İlan okunamadı: ${job.error}`)];
  const source = SOURCE_NAMES[job.source] || job.source;
  return [
    card(job.title,
      kv([["Şirket", job.company || null], ["Yer", job.location || null], ["Remote", job.remote ? "evet" : "hayır"],
        ["Puan", job.score != null ? h("b", { class: `score ${scoreClass(job.score)}` }, `${job.score} / 100`) : "henüz yok"],
        ...(job.score_reason ? [["Gerekçe (Oracle)", job.score_reason]] : []),
        ...(job.track ? [["Hat", TRACK_NAMES[job.track] || job.track]] : []),
        ["Kaynak", source], ["Bulundu", dateTime(job.found_at)], ["Durum", JOB_STATUS_NAMES[job.status] || job.status],
        ...(job.filter_reason ? [["Elenme", job.filter_reason]] : [])]),
      // Only https links are stored (POST /jobs/import); the page opens them in a new tab.
      h("a", { class: "icon-btn job-link", href: job.url, target: "_blank", rel: "noopener noreferrer" }, `İlanı ${source}'da aç ↗`)),
    card("Açıklama", job.description
      ? h("div", { class: "job-desc" }, job.description)
      : h("div", { class: "muted small" }, state.jobDetails[id] ? "Uyarı e-postası açıklama içermiyor; paket için ilanı açıp metnini aşağıya yapıştır." : "Okunuyor…")),
    ...(state.jobDetails[id] && job.status !== "filtered" ? [packageCard(job), trackCard(job)] : []),
  ];
}

// ------------------------------------------------------------------
// application package (J-5, plan-j G-I): JARVIS/Gandalf CV, Willy letter,
// Gordon check. Saving proposes Obsidian notes (approved); applying stays manual.
// ------------------------------------------------------------------

const PASTE_MAX = 20000;
const pasteInputs = {}; // job_id -> textarea, kept across renders like the chat input
const PACKAGE_STATUS = { running: "Yazılıyor… (CV → ön yazı → Gordon kontrolü, 1-2 dk)", ready: "Hazır · Gordon: desteksiz ifade yok",
  needs_fix: "Gözden geçir · Gordon uyarı verdi", failed: "Başarısız" };
const LANGUAGE_NAMES = [["auto", "İlanın dili"], ["tr", "Türkçe"], ["en", "İngilizce"]];
const packageLanguage = {}; // job_id -> auto | tr | en

function pasteInput(id) {
  if (!pasteInputs[id]) {
    pasteInputs[id] = h("textarea", { class: "chat-input", rows: 5, maxlength: PASTE_MAX,
      placeholder: "İlan metnini buraya yapıştır (gereksinimler, sorumluluklar)…" });
  }
  return pasteInputs[id];
}

async function startPackage(job) {
  const description = (pasteInputs[job.job_id]?.value || "").trim();
  await act(`/jobs/${encodeURIComponent(job.job_id)}/package`,
    { description, letter: true, language: packageLanguage[job.job_id] || "auto" });
  if (pasteInputs[job.job_id]) pasteInputs[job.job_id].value = "";
  await loadJob(job.job_id);
  return "Paket hazırlanıyor; bu panel kendini yeniler.";
}

function packageCard(job) {
  const pkg = job.package;
  const id = encodeURIComponent(job.job_id);
  const running = pkg?.status === "running";
  const parts = [];
  if (pkg) {
    parts.push(kv([["Durum", PACKAGE_STATUS[pkg.status] || pkg.status],
      ...(pkg.error ? [["Hata", pkg.error]] : []),
      ...((pkg.steps || []).length ? [["Adımlar", pkg.steps.map((s) => `${s.worker} (${s.model || "?"}, ${s.total_tokens ?? "?"} tok)`).join(" → ")]] : [])]));
    const review = pkg.review;
    if (review && (review.unsupported?.length || review.notes?.length || review.verdict !== "ok")) {
      parts.push(h("div", { class: `note${review.verdict === "ok" ? "" : " st-failed"}` },
        review.verdict === "unknown" ? h("div", {}, "Gordon'un cevabı okunamadı: metni kendin kontrol et.") : null,
        ...(review.unsupported || []).map((x) => h("div", {}, `✕ CV'de dayanağı yok: ${x}`)),
        ...(review.notes || []).map((x) => h("div", {}, `• ${x}`))));
    }
    if (review?.fixed?.length) {
      parts.push(h("div", { class: "note" }, h("div", {}, `Gordon'un bulduğu ${review.fixed.length} dayanaksız ifade yeniden yazdırıldı:`),
        ...review.fixed.map((x) => h("div", {}, `✓ ${x}`))));
    }
    if (review?.missing?.length) {
      parts.push(h("div", { class: "note" }, h("div", {}, "İlanda isteniyor, CV'nde yok (bilgi):"),
        ...review.missing.map((x) => h("div", {}, `– ${x}`))));
    }
    for (const [part, label] of [["cv", "Uyarlanmış CV"], ["letter", "Ön yazı"]]) {
      if (!pkg[part]) continue;
      const name = `${label === "Ön yazı" ? "Ön yazı" : "CV"} - ${job.company || "ilan"} - ${job.title}`;
      parts.push(h("div", { class: "row pkg-label" },
        h("span", { class: "small muted" }, label),
        h("span", { class: "row pkg-actions" },
          h("button", { type: "button", class: "icon-btn",
            onclick: () => (part === "cv" && pkg.cv_data ? printCv(pkg.cv_data, name) : printDocument(pkg[part], name)) }, "PDF"),
          // Same origin and session: the browser saves the file the orchestrator names.
          h("a", { class: "icon-btn", href: `/jobs/${id}/package/${part}.docx`, download: "" }, "Word"))),
        h("div", { class: "job-desc" }, pkg[part]));
    }
  }
  if (!running) {
    if (!job.description) parts.push(pasteInput(job.job_id));
    parts.push(h("div", { class: "row pkg-actions" },
      h("select", { class: "icon-btn", "aria-label": "Paket dili",
        onchange: (e) => { packageLanguage[job.job_id] = e.target.value; } },
        ...LANGUAGE_NAMES.map(([key, label]) => h("option", { value: key, selected: (packageLanguage[job.job_id] || "auto") === key }, label))),
      confirmable(`pkg-make-${job.job_id}`, pkg ? "Yeniden hazırla" : "Paket hazırla",
        "3 model çağrısı (JARVIS/Gandalf, Willy, Gordon). Başlasın mı?", () => startPackage(job)),
      ...(pkg && ["ready", "needs_fix"].includes(pkg.status) ? [confirmable(`pkg-save-${job.job_id}`, "Obsidian'a kaydet",
        "Kariyer/Başvurular altına not önerilir; Onaylar'dan onaylayınca yazılır. Devam?",
        async () => {
          const answer = await act(`/jobs/${id}/package/save`);
          return `${answer.notes.length} not önerildi: Onaylar bölümünden onayla.`;
        }, { tone: pkg.status === "ready" ? "ok" : "" })] : [])));
  }
  parts.push(noticeEl(`pkg-make-${job.job_id}`, `pkg-save-${job.job_id}`));
  parts.push(h("div", { class: "muted small" }, "Hiçbir şey gönderilmez: başvuruyu sen yaparsın. CV yalnız Kariyer/CV.md'deki bilgilerden yazılır."));
  return card("Başvuru paketi", ...parts);
}

// PDF: the browser's own "save as PDF" on a print view of one document (no
// library). Markdown from the models -> elements (never innerHTML).
function inlineMd(text) {
  const out = [];
  const plain = text.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1").replace(/`/g, "");
  plain.split(/(\*\*[^*]+\*\*)/).forEach((piece) => {
    if (/^\*\*[^*]+\*\*$/.test(piece)) out.push(h("b", {}, piece.slice(2, -2)));
    else if (piece) out.push(piece.replace(/(^|[^\w*])\*(\S[^*]*?\S|\S)\*(?![\w*])/g, "$1$2"));
  });
  return out;
}

function markdownBlocks(text) {
  const blocks = [];
  let para = [], list = null;
  const flush = () => {
    if (para.length) blocks.push(h("p", {}, ...inlineMd(para.join(" "))));
    para = [];
  };
  for (const raw of (text || "").replace(/\r\n/g, "\n").split("\n")) {
    const line = raw.trimEnd();
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    const bullet = line.match(/^\s*(?:[-*•]|\d{1,2}[.)])\s+(.*)$/);
    if (!bullet) list = null;
    if (!line.trim() || /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flush(); continue; }
    if (heading) {
      flush();
      blocks.push(h(heading[1].length === 1 && !blocks.length ? "h1" : heading[1].length <= 2 ? "h2" : "h3", {}, ...inlineMd(heading[2])));
    } else if (bullet) {
      flush();
      if (!list) { list = h("ul", {}); blocks.push(list); }
      list.append(h("li", {}, ...inlineMd(bullet[1])));
    } else {
      para.push(line.trim());
      if (raw.endsWith("  ")) flush();
    }
  }
  flush();
  return blocks;
}

function printDocument(text, name, area = h("article", { id: "print-area" }, ...markdownBlocks(text)), page = "") {
  document.getElementById("print-area")?.remove();
  document.body.append(area);
  // A page rule of its own (the CV's margins): named @page rules are not
  // honoured by every browser (Firefox printed the CV with the default margins).
  const pageStyle = page ? h("style", {}, `@media print { @page { ${page} } }`) : null;
  if (pageStyle) document.head.append(pageStyle);
  const title = document.title;
  document.title = name.replace(/[\\/:*?"<>|]+/g, "-"); // the PDF's suggested file name
  document.body.classList.add("printing");
  const done = () => {
    document.body.classList.remove("printing");
    document.title = title;
    area.remove();
    pageStyle?.remove();
    window.removeEventListener("afterprint", done);
  };
  window.addEventListener("afterprint", done);
  window.print();
}

// The tailored CV in the user's own CV design (same as docx_export.cv_docx):
// photo top right, teal headings with a rule, companies in the accent colour.
const CV_HEADINGS = {
  tr: { summary: "ÖZET", projects: "PROJELER", experience: "DENEYİM", skills: "YETENEKLER",
    education: "EĞİTİM VE SERTİFİKALAR", references: "REFERANSLAR", languages: "Diller" },
  en: { summary: "SUMMARY", projects: "PROJECTS", experience: "EXPERIENCE", skills: "SKILLS",
    education: "EDUCATION & CERTIFICATIONS", references: "REFERENCES", languages: "Languages" },
};

function cvSheet(cv, photo) {
  const names = CV_HEADINGS[cv.language] || CV_HEADINGS.tr;
  const sep = () => h("span", { class: "cv-sep" }, " — ");
  const has = (section) => (Array.isArray(cv[section]) ? cv[section].length : Boolean(cv[section]))
    || (section === "skills" && Boolean(cv.languages));
  const bullets = (list) => (list?.length ? h("ul", {}, ...list.map((b) => h("li", {}, b))) : null);
  const body = {
    summary: () => [h("p", {}, cv.summary)],
    references: () => [h("p", { class: "cv-ref" }, cv.references)],
    projects: () => cv.projects.map((p) => h("div", { class: "cv-item" },
      h("p", {}, h("b", {}, p.name), p.description ? [sep(), p.description] : null), bullets(p.bullets))),
    experience: () => cv.experience.map((e) => h("div", { class: "cv-item" },
      h("p", { class: "cv-role" }, h("b", {}, e.title), e.company ? [sep(), h("b", { class: "cv-org" }, e.company)] : null),
      [e.dates, e.location].some(Boolean) ? h("p", { class: "cv-when" }, [e.dates, e.location].filter(Boolean).join("  |  ")) : null,
      bullets(e.bullets))),
    skills: () => [...cv.skills.map((s) => h("p", {}, s.label ? `${s.label}: ${s.text}` : s.text)),
      cv.languages ? h("p", {}, h("b", {}, `${names.languages}:`), ` ${cv.languages}`) : null],
    education: () => cv.education.map((e) => h("p", {}, h("b", {}, e.title),
      e.org ? [sep(), h("b", { class: "cv-org" }, e.org)] : null, e.dates ? h("i", { class: "cv-when" }, `  (${e.dates})`) : null)),
  };
  return h("article", { id: "print-area", class: "cv-sheet" },
    h("header", {},
      h("div", {},
        h("h1", {}, cv.name),
        cv.headline ? h("p", { class: "cv-headline" }, cv.headline) : null,
        ...(cv.contact || []).map((line) => h("p", { class: "cv-contact" }, line.join("  •  ")))),
      photo ? h("img", { src: photo.src, alt: "" }) : null),
    ...(cv.order || []).filter(has).map((section) => h("section", {}, h("h2", {}, names[section]), ...body[section]())));
}

async function printCv(cv, name) {
  // The photo first, so the print never has an empty frame; no photo: no frame.
  const photo = await new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => resolve(null);
    img.src = "/career/photo";
  });
  const sheet = cvSheet(cv, photo);
  fitOnePage(sheet);
  printDocument("", name, sheet, CV_PAGE);
}

// One A4 page (297 mm less the 9 + 10 mm margins, 180 mm wide): measured off
// screen in the fonts this device has, the base size goes down to 7.5 pt at most.
// A little to spare under 278 mm: the print layout breaks lines and pages
// slightly differently from the screen measure.
const CV_PAGE = "size: A4; margin: 9mm 15mm 10mm;";
const CV_PAGE_MM = { width: 180, height: 270 };

function fitOnePage(sheet) {
  const mm = 96 / 25.4;
  sheet.style.cssText = `display:block;position:fixed;left:-10000px;top:0;width:${CV_PAGE_MM.width}mm`;
  document.body.append(sheet);
  for (let size = 9.5; size >= 7.5; size -= 0.25) {
    sheet.style.fontSize = `${size}pt`;
    if (sheet.getBoundingClientRect().height <= CV_PAGE_MM.height * mm) break;
  }
  const fontSize = sheet.style.fontSize;
  sheet.remove();
  sheet.style.cssText = "";
  sheet.style.fontSize = fontSize;
}

const USER_STATUSES = [["applied", "Başvurdum"], ["interview", "Mülakat"], ["rejected", "Red"], ["offer", "Teklif"], ["dismissed", "Gizle"], ["new", "Yeniye al"]];

function trackCard(job) {
  const id = encodeURIComponent(job.job_id);
  return card("Takip",
    h("div", { class: "row pkg-actions" }, ...USER_STATUSES.filter(([key]) => key !== job.status).map(([key, label]) =>
      confirmable(`job-status-${job.job_id}-${key}`, label, `Durum: ${JOB_STATUS_NAMES[key]}?`, async () => {
        await act(`/jobs/${id}/status`, { status: key });
        await loadJob(job.job_id);
        return `Durum: ${JOB_STATUS_NAMES[key]}`;
      }))),
    noticeEl(`job-status-${job.job_id}`));
}

// ------------------------------------------------------------------
// side panel
// ------------------------------------------------------------------

function select(target) {
  state.selected = target;
  $("side").closest(".shell").classList.remove("side-collapsed");
  $("side-open").hidden = true;
  if (target?.type === "task" && !state.details[target.id]) loadDetails().then(render);
  if (target?.type === "job" && !state.jobDetails[target.id]) loadJob(target.id).then(render);
  if (target?.type === "agent" && state.agentTab === "sohbet") loadChat(target.id).then(render);
  render();
}

function kv(pairs) {
  return h("dl", { class: "kv" }, pairs.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v ?? "–")]));
}

function card(title, ...body) {
  return h("div", { class: "card" }, h("h4", {}, title), ...body);
}

function findTask(id) {
  if (!id) return null;
  for (const task of state.tasks) if (task.task_id === id) return task;
  for (const detail of Object.values(state.details)) {
    if (detail.task?.task_id === id) return detail.task;
    for (const child of detail.children || []) if (child.task_id === id) return child;
  }
  return null;
}

function agentHistory(id) {
  const seen = new Map();
  for (const task of state.tasks) if (task.worker === id) seen.set(task.task_id, task);
  for (const detail of Object.values(state.details)) {
    for (const child of detail.children || []) if (child.worker === id) seen.set(child.task_id, child);
  }
  return [...seen.values()].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
}

function agentPanel(id, views) {
  const entry = views.find((v) => v.worker.id === id);
  if (!entry) return [h("p", { class: "muted" }, "Bu ajan artık listede yok.")];
  const { worker, view } = entry;
  const c = character(id);
  const task = view.task && (findTask(view.task.task_id) || view.task);
  const tokens = worker.daily_tokens;
  const history = agentHistory(id);
  const done = history.filter((t) => FINAL.has(t.status));
  const ok = done.filter((t) => t.status === "completed").length;
  const usage = state.usage?.agents?.[id];
  const events = state.activity.filter((e) => (e.worker || e.agent) === id && !isNoise(e)).slice(-20).reverse();

  const header = [
    h("div", { class: "profile" },
      h("span", { class: "avatar", style: `--agent:${c.color}` }, characterSvg(id)),
      h("div", {}, h("h3", {}, worker.name), h("div", { class: "muted" }, id === "obsidian" ? "Not / vault servisi (LLM değil)" : roleLabel(worker.role)))),
    h("div", { class: "tabs", role: "tablist" }, AGENT_TABS.map(([key, label]) => h("button", {
      type: "button", role: "tab", class: `tab${state.agentTab === key ? " active" : ""}`, "aria-selected": String(state.agentTab === key),
      onclick: () => { state.agentTab = key; if (key === "sohbet") loadChat(id).then(render); render(); },
    }, label))),
  ];

  if (state.agentTab === "sohbet") return [...header, ...chatTab(id)];

  if (state.agentTab === "aktivite") {
    return [...header,
      card("Canlı olaylar",
        events.length ? h("ul", { class: "timeline" }, events.map((e) => h("li", {}, `${time(e.timestamp)} ${eventText(e)}`)))
          : h("div", { class: "muted small" }, "Bu oturumda olay yok.")),
      history.length > 0 && card("Son görevleri",
        h("ul", { class: "timeline" }, history.slice(0, 8).map((t) => h("li", { style: "cursor:pointer", onclick: () => select({ type: "task", id: t.parent_id || t.task_id }) },
          chip(t.status), " ", time(t.created_at), " · ", clip(t.message, 70))))),
    ];
  }

  if (state.agentTab === "performans") {
    return [...header,
      tokens && card("Bugünkü bütçe",
        h("div", { class: "small" }, `${num(tokens.used)} / ${num(tokens.budget)} bulut token${usage ? ` · ${usage.calls} çağrı` : ""}`),
        h("div", { class: "mini-bar" }, h("div", { style: `width:${Math.min(100, tokens.budget ? (tokens.used / tokens.budget) * 100 : 0)}%` }))),
      usage && card("Bugünkü kullanım", kv([
        ["Çağrı", usage.calls], ["Bulut token", num(usage.cloud_tokens)], ["Yerel token", num(usage.local_tokens)],
      ])),
      id !== "obsidian" && card("Performans (görünen son görevler)",
        done.length
          ? kv([["Biten", done.length], ["Başarılı", ok], ["Başarısız", done.filter((t) => t.status === "failed").length]])
          : h("div", { class: "muted small" }, "Son 15 görev içinde bu ajana ait biten görev yok."),
        h("div", { class: "note" }, "Yalnız panelin yüklediği görevlerden sayılır; tam metrikler Faz L'de.")),
    ];
  }

  return [...header,
    card("Durum",
      kv([
        ["Durum", STATUS_LABEL[view.status]],
        ["Neden", view.reason],
        ["Oda", ROOMS.find((r) => r.id === view.room)?.name],
        ["Model", worker.model || (id === "obsidian" ? "yok" : "bilinmiyor")],
        ["Son işlem", id === "obsidian" ? (view.lastAt ? time(new Date(view.lastAt).toISOString()) : "bu oturumda yok") : history[0] ? dateTime(history[0].created_at) : "kayıt yok"],
      ])),
    task && card("Şu anki görev",
      kv([["Durum", chip(task.status)], ["Adım", task.step || "–"], ["Başladı", dateTime(task.created_at)]]),
      task.message ? h("div", { class: "pre" }, clip(task.message, 600)) : null,
      h("button", { type: "button", class: "icon-btn", onclick: () => select({ type: "task", id: task.parent_id || task.task_id }) }, "Görevi aç")),
    tokens && card("Bugünkü bütçe",
      h("div", { class: "small" }, `${num(tokens.used)} / ${num(tokens.budget)} bulut token${usage ? ` · ${usage.calls} çağrı` : ""}`),
      h("div", { class: "mini-bar" }, h("div", { style: `width:${Math.min(100, tokens.budget ? (tokens.used / tokens.budget) * 100 : 0)}%` }))),
    h("div", { class: "note" }, "Profil salt okunur (personality dosyaları arayüzden değişmez)."),
  ];
}

// ------------------------------------------------------------------
// chat with a worker (K-5, plan-k karar I): no tools, one call per message
// ------------------------------------------------------------------

const AGENT_TABS = [["genel", "Genel"], ["sohbet", "Sohbet"], ["aktivite", "Aktivite"], ["performans", "Performans"]];
const CHAT_MAX = 4000;
const chatInput = h("textarea", { class: "chat-input", rows: 3, maxlength: CHAT_MAX, placeholder: "Mesaj · Enter gönderir, Shift+Enter yeni satır" });
const chatLog = h("div", { class: "chat-log", "aria-live": "polite" });
const chatFoot = h("div", { class: "row chat-foot" });
const chatBox = h("div", { class: "chat" }, chatLog, chatInput, chatFoot);
let chatWorker = null;

const canChat = (id) => id !== "ares" && id !== "obsidian";
const chatOf = (id) => (state.chat[id] ||= { conversation_id: null, turns: [], fresh: false, loaded: false, sending: false, error: "" });

async function loadChat(id) {
  if (!canChat(id)) return;
  const chat = chatOf(id);
  if (chat.fresh) return;
  try {
    const query = chat.conversation_id ? `?conversation_id=${encodeURIComponent(chat.conversation_id)}` : "";
    const data = await api(`/workers/${encodeURIComponent(id)}/chat${query}`);
    Object.assign(chat, { conversation_id: data.conversation_id, turns: data.turns || [], loaded: true, error: "" });
  } catch (exc) {
    chat.error = `Sohbet okunamadı: ${exc.message}`;
  }
}

// Called by poll(): only the open chat, and only while an answer is coming or before the first load.
async function pollChat() {
  const sel = state.selected;
  if (sel?.type !== "agent" || state.agentTab !== "sohbet" || !canChat(sel.id)) return;
  const chat = chatOf(sel.id);
  if (!chat.loaded || chat.turns.some((t) => t.status === "running")) await loadChat(sel.id);
}

async function sendChat(id) {
  const chat = chatOf(id);
  const message = chatInput.value.trim();
  if (!message || chat.sending || chat.turns.some((t) => t.status === "running")) return;
  chat.sending = true;
  chat.error = "";
  renderChat(id);
  try {
    const answer = await act(`/workers/${encodeURIComponent(id)}/chat`, { message, conversation_id: chat.conversation_id });
    chatInput.value = "";
    Object.assign(chat, { conversation_id: answer.conversation_id, fresh: false });
    await loadChat(id);
  } catch (exc) {
    chat.error = `Gönderilemedi: ${exc.message}`;
  }
  chat.sending = false;
  renderChat(id);
}

function renderChat(id) {
  const chat = chatOf(id);
  const name = state.workers.find((w) => w.id === id)?.name || id;
  const atBottom = chatLog.scrollHeight - chatLog.scrollTop - chatLog.clientHeight < 40;
  const turns = chat.turns.flatMap((t) => [
    h("div", { class: "chat-msg me" }, h("div", { class: "who" }, `Sen · ${time(t.created_at)}`), t.message),
    t.status === "running"
      ? h("div", { class: "chat-msg them typing" }, h("div", { class: "who" }, name), "yazıyor…")
      : t.status === "completed"
        ? h("div", { class: "chat-msg them" },
          h("div", { class: "who" }, `${name} · ${time(t.completed_at)}${t.model ? ` · ${t.model}` : ""}${t.total_tokens != null ? ` · ${num(t.total_tokens)} token` : ""}`),
          t.reply || "(boş yanıt)")
        : h("div", { class: "chat-msg them failed" }, h("div", { class: "who" }, `${name} · ${TASK_STATUS_LABEL[t.status] || t.status}`), t.error || "yanıt yok"),
  ]);
  chatLog.replaceChildren(...(turns.length ? turns : [h("div", { class: "muted small" }, chat.loaded || chat.fresh ? "Henüz mesaj yok." : "Yükleniyor…")]));
  if (atBottom || chat.sending) chatLog.scrollTop = chatLog.scrollHeight;
  const busy = chat.sending || chat.turns.some((t) => t.status === "running");
  chatInput.disabled = chat.sending;
  chatFoot.replaceChildren(
    h("button", { type: "button", class: "icon-btn ok", disabled: busy || !chatInput.value.trim() || !state.connected, onclick: () => sendChat(id) }, chat.sending ? "…" : "Gönder"),
    h("button", { type: "button", class: "icon-btn", disabled: busy, onclick: () => {
      Object.assign(chat, { conversation_id: null, turns: [], fresh: true, error: "" });
      renderChat(id);
    } }, "Yeni sohbet"),
    h("span", { class: "muted small" }, `${chatInput.value.trim().length} / ${CHAT_MAX}`),
    ...(chat.error ? [h("div", { class: "note st-failed" }, chat.error)] : []),
  );
}

function chatTab(id) {
  if (id === "ares") {
    return [card("Sohbet", h("div", { class: "small" }, "Ares'le konuşmak Mission Console'dan yapılır: verdiğin her mesaj gerçek bir görev olur."),
      h("div", { class: "row" }, h("button", { type: "button", class: "icon-btn ok", onclick: () => { $("console").scrollIntoView({ behavior: reduceMotion.matches ? "auto" : "smooth" }); $("console-text").focus(); } }, "Mission Console'a git")))];
  }
  if (id === "obsidian") return [card("Sohbet", h("div", { class: "muted small" }, "Obsidian bir LLM değil, not servisidir; sohbet yok."))];
  chatWorker = id;
  renderChat(id);
  return [chatBox, h("div", { class: "note" }, "Araçsız sohbet: not yazamaz, arama yapamaz, kod çalıştıramaz. İş gerektiren istekleri Mission Console'dan Ares'e verin. Her mesaj bir LLM çağrısıdır ve bütçeye sayılır.")];
}

// J-6: the last review of the answer, and a button to ask Gordon (again).
function reviewCard(task, result) {
  if (task.parent_id || !["completed", "needs_review"].includes(task.status) || !result.response) return null;
  const review = result.review;
  const id = task.task_id;
  return card("İnceleme",
    task.status === "needs_review" ? h("div", { class: "note st-needs_review" },
      "İnceleyen bu yanıtı onaylamadı: bitmiş sayılmaz, kendin gözden geçir ya da yeniden incelet.") : null,
    review ? kv([["İnceleyen", review.worker || "?"], ["Karar", review.verdict || review.error || "yok"],
      ...(review.summary ? [["Özet", review.summary]] : [])]) : h("div", { class: "muted small" }, "Bu yanıt incelenmedi."),
    ...(review?.findings || []).map((x) => h("div", { class: "small" }, `• ${x}`)),
    state.reviewing[id] ? h("div", { class: "muted small" }, "Gordon inceliyor…")
      : h("div", { class: "row" }, confirmable(`review:${id}`, review ? "Yeniden incelet" : "Gordon'a incelet",
        "Gordon yanıtı incelesin mi? (1 model çağrısı)", async () => {
          await act(`/tasks/${encodeURIComponent(id)}/review`);
          state.reviewing[id] = new Date().toISOString();
          return "İnceleme başladı; sonuç bu panelde görünecek.";
        })),
    noticeEl(`review:${id}`));
}

function taskPanel(id) {
  const detail = state.details[id];
  const task = detail?.task || findTask(id);
  if (!task) return [h("p", { class: "muted" }, "Görev ayrıntısı yükleniyor veya okunamadı.")];
  const result = task.result || {};
  const children = detail?.children || [];
  const approvals = detail?.approvals || [];
  const events = (detail?.activity || []).filter((e) => state.showAll || !isNoise(e));
  return [
    card("Görev",
      kv([
        ["Durum", chip(task.status)],
        ["Ajan", task.worker],
        ["Oluşturuldu", dateTime(task.created_at)],
        ["Bitti", dateTime(task.completed_at)],
        ["Süre", duration(task) || "–"],
        ["Kimlik", task.task_id],
      ])),
    card("İstek", h("div", { class: "pre" }, task.message || "–")),
    !FINAL.has(task.status) && card("İşlem",
      h("div", { class: "row" }, confirmable(`cancel:${task.task_id}`, "Görevi iptal et", "Görev iptal edilsin mi?", async () => {
        await act(`/tasks/${encodeURIComponent(task.task_id)}/cancel`);
        return "Görev iptal edildi.";
      }, { tone: "danger" })), noticeEl("cancel:")),
    task.error && card("Hata", h("div", { class: "pre" }, task.error)),
    result.response && card("Ares'in yanıtı", h("div", { class: "pre" }, result.response)),
    reviewCard(task, result),
    children.length > 0 && card(`Ajan görevleri (${children.length})`,
      h("ul", { class: "timeline" }, children.map((c) => h("li", {},
        chip(c.status), ` ${c.worker}${c.step ? ` · ${c.step}` : ""}${c.depends_on?.length ? " · bağımlı" : ""}`,
        c.result?.summary ? h("div", { class: "muted" }, clip(c.result.summary, 220)) : null,
        c.error ? h("div", { class: "st-failed" }, clip(c.error, 220)) : null)))),
    approvals.length > 0 && card("Onaylar",
      h("ul", { class: "timeline" }, approvals.map(approvalItem)), noticeEl("approve:", "reject:")),
    card(`Olaylar (${events.length})`,
      events.length ? h("ul", { class: "timeline" }, events.map((e) => h("li", {}, `${time(e.timestamp)} [${eventAgent(e)}] ${eventText(e)}`)))
        : h("div", { class: "muted small" }, detail ? "Kayıtlı olay yok." : "Ayrıntı yüklenmedi.")),
  ];
}

// ------------------------------------------------------------------
// actions (K-4). Each one: button -> "Evet / Vazgeç" -> the orchestrator's
// own endpoint. Through the gateway the same session + X-SAVAS-CSRF (A5).
// ------------------------------------------------------------------

async function act(path, body) {
  const response = await fetch(path, {
    method: "POST", credentials: "same-origin", cache: "no-store",
    headers: { "content-type": "application/json", "x-savas-csrf": "1" },
    body: JSON.stringify(body || {}),
  });
  if (response.status === 401) {
    location.href = `/login?next=${encodeURIComponent(location.pathname)}`;
    throw new Error("oturum gerekli");
  }
  let data = {};
  try {
    data = await response.json();
  } catch {
    /* an error page without JSON */
  }
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${response.status}`);
  return data;
}

async function runAction(key, run) {
  state.confirm = null;
  state.busy = key;
  render();
  try {
    state.notice = { key, text: await run(), error: false };
  } catch (exc) {
    state.notice = { key, text: `Hata: ${exc.message || exc}`, error: true };
  }
  state.busy = null;
  await poll();
}

function confirmable(key, label, question, run, { tone = "", disabled = false } = {}) {
  if (state.busy === key) return h("button", { type: "button", class: "icon-btn", disabled: true }, "…");
  if (state.confirm === key) {
    return h("span", { class: "confirm" },
      h("span", { class: "small" }, question),
      h("button", { type: "button", class: `icon-btn ${tone}`, onclick: () => runAction(key, run) }, "Evet"),
      h("button", { type: "button", class: "icon-btn", onclick: () => { state.confirm = null; render(); } }, "Vazgeç"));
  }
  return h("button", {
    type: "button", class: `icon-btn ${tone}`, disabled: disabled || Boolean(state.busy),
    onclick: () => { state.confirm = key; render(); },
  }, label);
}

// The last action's result, where its key starts with one of the prefixes.
function noticeEl(...prefixes) {
  const notice = state.notice;
  if (!notice || !prefixes.some((p) => notice.key.startsWith(p))) return null;
  return h("div", { class: `note${notice.error ? " st-failed" : ""}` }, notice.text);
}

function approvalItem(a) {
  const id = encodeURIComponent(a.approval_id);
  const args = a.arguments || {};
  return h("li", {},
    h("div", {}, h("b", {}, `${a.agent}/${a.action}`), ` · ${dateTime(a.created_at)}`),
    h("div", { class: "pre small" }, clip(JSON.stringify(args, null, 1), 600)),
    a.status === "pending"
      ? h("div", { class: "row" },
        confirmable(`approve:${a.approval_id}`, "Onayla", "İşlem yürütülsün mü?", async () => {
          await act(`/approvals/${id}/approve`);
          return `${a.agent}/${a.action} onaylandı.`;
        }, { tone: "ok" }),
        confirmable(`reject:${a.approval_id}`, "Reddet", "Reddedilsin mi?", async () => {
          await act(`/approvals/${id}/reject`);
          return `${a.agent}/${a.action} reddedildi.`;
        }, { tone: "danger" }),
        h("button", { type: "button", class: "icon-btn", onclick: () => select({ type: "task", id: a.task_id }) }, "Görev"))
      : h("div", { class: "muted small" }, a.status));
}

function memoryCard() {
  if (state.session) {
    return card("Hafıza çakışmaları", h("div", { class: "muted small" }, "Yalnız yerel erişimde (127.0.0.1:18000); giriş kapısı /memory'yi geçirmez."));
  }
  if (!state.flagged?.length) return null;
  return card(`Hafıza çakışmaları (${state.flagged.length})`,
    h("ul", { class: "timeline" }, state.flagged.map((fact) => {
      const old = state.activeFacts.find((f) => f.key_norm === fact.key_norm && f.scope === fact.scope);
      const resolve = (choice, text) => async () => {
        await act(`/memory/facts/${encodeURIComponent(fact.fact_id)}/resolve`, { choice });
        return `"${fact.key}": ${text}.`;
      };
      return h("li", {},
        h("b", {}, fact.key), ` · ${fact.scope} · ${fact.source}`,
        h("div", { class: "small" }, "Eski: ", old ? old.value : "bulunamadı"),
        h("div", { class: "small" }, "Yeni: ", fact.value),
        h("div", { class: "row" },
          confirmable(`keep:${fact.fact_id}`, "Eskiyi koru", "Yeni bilgi bırakılsın mı?", resolve("keep_old", "eski korundu")),
          confirmable(`new:${fact.fact_id}`, "Yeniyi kullan", "Eski bilgi değiştirilsin mi?", resolve("use_new", "yeni bilgi kullanılıyor"), { tone: "ok" })));
    })), noticeEl("keep:", "new:"));
}

// Mission Console: a message to Ares as a background task (karar J).
function renderConsole() {
  const text = $("console-text").value.trim();
  const send = confirmable("send", "Ares'e gönder", "Gönderilsin mi? (LLM çağrısı, bütçeye sayılır)", async () => {
    const message = $("console-text").value.trim();
    const { task_id: id } = await act("/task", { message, background: true, idempotency_key: crypto.randomUUID() });
    $("console-text").value = "";
    if (id) select({ type: "task", id });
    return "Görev başladı; ilerleme sağ panelde.";
  }, { tone: "ok", disabled: !text || !state.connected });
  $("console-actions").replaceChildren(send, h("span", { class: "muted small" }, `${text.length} / 4000`));
  const note = noticeEl("send");
  $("console-note").hidden = !note;
  $("console-note").replaceChildren(...(note ? [note] : []));
}

// ------------------------------------------------------------------
// Web Push (K-3f, docs/plan-k.md Ek B). Only through the gateway.
// ------------------------------------------------------------------

const pushSupported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

function b64ToBytes(text) {
  const raw = atob((text + "=".repeat((4 - (text.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function postJson(path, body) {
  return fetch(path, {
    method: "POST", credentials: "same-origin", cache: "no-store",
    headers: { "content-type": "application/json", "x-savas-csrf": "1" },
    body: JSON.stringify(body || {}),
  });
}

async function refreshPush() {
  if (!state.session) return;
  try {
    const response = await fetch("/push/status", { cache: "no-store", credentials: "same-origin" });
    if (!response.ok) return;
    state.push.enabled = (await response.json()).enabled;
    if (pushSupported()) {
      const registration = await navigator.serviceWorker.getRegistration("/ui/");
      state.push.subscribed = Boolean(await registration?.pushManager.getSubscription());
      state.push.permission = Notification.permission;
    }
  } catch {
    /* the card says what it knows */
  }
  render();
}

async function pushAction(work) {
  state.push.busy = true;
  render();
  try {
    state.push.message = await work();
  } catch (exc) {
    state.push.message = `Hata: ${exc.message || exc}`;
  }
  state.push.busy = false;
  await refreshPush();
}

function enablePush() {
  // iOS: the permission prompt must start in the tap itself, before any await.
  const asked = Notification.requestPermission();
  pushAction(async () => {
    const permission = await asked;
    state.push.permission = permission;
    if (permission !== "granted") return "Bildirim izni verilmedi.";
    const registration = await navigator.serviceWorker.register("/ui/sw.js", { scope: "/ui/" });
    await navigator.serviceWorker.ready;
    const keyResponse = await fetch("/push/key", { cache: "no-store", credentials: "same-origin" });
    if (!keyResponse.ok) throw new Error(`anahtar alınamadı (HTTP ${keyResponse.status})`);
    const { key } = await keyResponse.json();
    const subscription = (await registration.pushManager.getSubscription())
      || await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(key) });
    const saved = await postJson("/push/subscribe", subscription.toJSON());
    if (!saved.ok) throw new Error(`abonelik kaydedilemedi (HTTP ${saved.status})`);
    return "Bildirimler açık. Test ile deneyebilirsiniz.";
  });
}

function disablePush() {
  pushAction(async () => {
    const registration = await navigator.serviceWorker.getRegistration("/ui/");
    const subscription = await registration?.pushManager.getSubscription();
    if (subscription) {
      await postJson("/push/unsubscribe", { endpoint: subscription.endpoint });
      await subscription.unsubscribe();
    }
    return "Bu cihazın bildirimleri kapatıldı.";
  });
}

function testPush() {
  pushAction(async () => {
    const response = await postJson("/push/test");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const { sent, statuses } = await response.json();
    const ok = statuses.filter((code) => code >= 200 && code < 300).length;
    return `${sent} aboneliğe gönderildi, ${ok} kabul edildi (${statuses.join(", ") || "–"}).`;
  });
}

function pushCard() {
  if (!state.session) return null;
  const p = state.push;
  const button = (label, onclick) => h("button", { type: "button", class: "icon-btn", disabled: p.busy, onclick }, label);
  let body;
  if (!p.enabled) {
    body = h("div", { class: "muted small" }, "Sunucuda kapalı: VAPID anahtarı yok (scripts/set_ui_push_keys.sh).");
  } else if (!pushSupported()) {
    body = h("div", { class: "muted small" }, "Bu tarayıcı Web Push desteklemiyor. iPhone'da ana ekrandaki SAVAS AI uygulamasından açın (iOS 16.4+).");
  } else if (p.permission === "denied") {
    body = h("div", { class: "muted small" }, "Bildirim izni reddedilmiş. iPhone: Ayarlar → Bildirimler → SAVAS AI.");
  } else if (p.subscribed) {
    body = h("div", {}, h("div", { class: "small" }, "Bu cihaz onay bildirimlerini alıyor."),
      h("div", { class: "row" }, button("Test", testPush), button("Kapat", disablePush)));
  } else {
    body = h("div", {}, h("div", { class: "small" }, "Onay beklendiğinde bu cihaza bildirim gönderilir (yalnız ajan/işlem ve saat)."),
      h("div", { class: "row" }, button("Bildirimleri aç", enablePush)));
  }
  return card("Bildirimler", body, p.message ? h("div", { class: "note" }, p.message) : null);
}

function summaryPanel() {
  const local = state.health?.local_llm;
  return [
    pushCard(),
    card("Bekleyen onaylar",
      state.approvals.length
        ? h("ul", { class: "timeline" }, state.approvals.map(approvalItem))
        : h("div", { class: "muted small" }, state.connected ? "Bekleyen onay yok." : "Bağlantı yok."),
      noticeEl("approve:", "reject:")),
    memoryCard(),
    card("Model durumu",
      kv([
        ["Kullanılamayan", (state.health?.unavailable_models || []).join(", ") || "yok"],
        ["Yerel LLM", local ? (local.reachable ? JSON.stringify(local.models) : "ulaşılamıyor") : "–"],
        ["Yerelde çalışan", (local?.agents_on_local || []).join(", ") || "yok"],
      ])),
    h("div", { class: "note" }, "Bir karaktere ya da göreve tıklayın. Karakterler odalara gerçek görev durumuna göre gider; boştakiler Dinlenme Odası'nda."),
  ];
}

function renderSide(views) {
  const sel = state.selected;
  let title = "Ofis özeti";
  let body;
  if (sel?.type === "agent") {
    title = views.find((v) => v.worker.id === sel.id)?.worker.name || "Ajan";
    body = agentPanel(sel.id, views);
  } else if (sel?.type === "task") {
    title = "Görev ayrıntısı";
    body = taskPanel(sel.id);
  } else if (sel?.type === "job") {
    title = "İlan";
    body = jobPanel(sel.id);
  } else {
    body = summaryPanel();
  }
  $("side-title").textContent = title;
  // The open chat updates in place: rebuilding would drop the focus and the half-typed text.
  if (sel?.type === "agent" && state.agentTab === "sohbet" && sel.id === chatWorker && $("side-body").contains(chatBox)) {
    renderChat(sel.id);
    return;
  }
  // Do not rebuild while the user is selecting text in the panel.
  if (getSelection()?.toString() && $("side-body").contains(getSelection().anchorNode)) return;
  // The rebuild keeps where the reader is: the panel and every scrolling box
  // in it (an answer, a timeline), as long as the same item is shown.
  const side = $("side-body");
  const shown = `${sel?.type}:${sel?.id}`;
  const kept = shown === lastSideShown
    ? new Map([...side.querySelectorAll(SCROLLERS)].filter((el) => el.scrollTop).map((el) => [scrollKey(el), el.scrollTop]))
    : new Map();
  const scroll = shown === lastSideShown ? side.scrollTop : 0;
  side.replaceChildren(...body.filter(Boolean));
  side.querySelectorAll(SCROLLERS).forEach((el) => { if (kept.has(scrollKey(el))) el.scrollTop = kept.get(scrollKey(el)); });
  side.scrollTop = scroll;
  lastSideShown = shown;
}

const SCROLLERS = ".pre, .timeline, .job-desc";
let lastSideShown = null;
// A box is known by its card's title and its class; the rebuilt panel has the same ones.
const scrollKey = (el) => `${el.closest(".card")?.querySelector("h4")?.textContent || ""}|${el.className}`;

// ------------------------------------------------------------------

function renderStatus() {
  const conn = $("conn");
  conn.className = `conn ${state.connected ? "ok" : state.polled ? "bad" : ""}`;
  $("conn-text").textContent = state.connected ? "bağlı" : state.polled ? "bağlantı yok" : "bağlanıyor…";
  $("last-update").textContent = state.lastOk ? `son veri ${state.lastOk.toLocaleTimeString("tr-TR")}` : "";
  const banner = $("banner");
  const partial = state.connected && state.partial?.length;
  banner.hidden = !state.polled || (state.connected && !partial);
  banner.classList.toggle("warn", Boolean(partial));
  banner.textContent = partial
    ? `Kısmi veri: ${state.partial.join(", ")} okunamadı; bu bölümlerde son alınan veri gösteriliyor.`
    : state.connected ? "" : `Orchestrator'a ulaşılamıyor (${state.error}). ${state.lastOk ? `Gösterilen son veri: ${state.lastOk.toLocaleTimeString("tr-TR")}.` : "Henüz veri alınmadı."}`;
  const pill = $("nav-approvals");
  pill.textContent = state.connected ? state.approvals.length : "–";
  pill.classList.toggle("hot", state.approvals.length > 0);
}

function render() {
  const views = agentViews();
  renderStatus();
  renderHud(views);
  renderOffice(views);
  renderEnergy(views);
  renderFeed();
  renderTasks();
  renderJobs();
  renderMetrics();
  renderGoals();
  renderSide(views);
  renderConsole();
}

// The gateway answers /session; the local orchestrator (127.0.0.1:18000/ui) does not.
async function showSession() {
  try {
    const response = await fetch("/session", { cache: "no-store", credentials: "same-origin" });
    if (!response.ok) return;
    const { user } = await response.json();
    state.session = user;
    refreshPush();
    $("session-user").textContent = `(${user})`;
    $("logout").hidden = false;
    $("access-note").textContent = "Giriş yapıldı";
  } catch {
    /* local access: no login, no logout button */
  } finally {
    state.sessionChecked = true;
  }
}

// ≤1180 px the side panel covers the page; ✕, Esc or a menu link closes it.
const narrow = matchMedia("(max-width: 1180px)");

function closeSide() {
  $("side").closest(".shell").classList.add("side-collapsed");
  $("side-open").hidden = false;
  requestAnimationFrame(renderLinks);
}

// Phone drawer (≤760 px): ☰ opens it; a link, the backdrop or Esc closes it.
function setMenu(open) {
  $("side").closest(".shell").classList.toggle("menu-open", open);
  $("backdrop").hidden = !open;
  $("menu-btn").setAttribute("aria-expanded", String(open));
  $("menu-btn").setAttribute("aria-label", open ? "Menüyü kapat" : "Menüyü aç");
}

function start() {
  buildOffice();
  showSession();
  $("console-text").addEventListener("input", renderConsole);
  chatInput.addEventListener("input", () => chatWorker && renderChat(chatWorker));
  chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (chatWorker) sendChat(chatWorker);
    }
  });
  $("console-form").addEventListener("submit", (e) => e.preventDefault());
  $("console-text").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && $("console-text").value.trim()) {
      e.preventDefault();
      state.confirm = "send";
      render();
    }
  });
  $("menu-btn").addEventListener("click", () => setMenu(!$("side").closest(".shell").classList.contains("menu-open")));
  $("backdrop").addEventListener("click", () => setMenu(false));
  // A menu link goes to the page: on a narrow screen the overlay panel steps aside.
  $("sidebar").addEventListener("click", (e) => {
    const link = e.target.closest("a.nav");
    if (!e.target.closest(".nav")) return;
    setMenu(false);
    if (link && narrow.matches) closeSide();
  });
  addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    setMenu(false);
    if (narrow.matches) closeSide();
  });
  $("feed-all").addEventListener("change", (e) => { state.showAll = e.target.checked; render(); });
  $("nav-summary").addEventListener("click", () => select(null));
  $("side-close").addEventListener("click", closeSide);
  $("side-open").addEventListener("click", () => select(state.selected));
  addEventListener("resize", () => requestAnimationFrame(renderLinks));
  // On a narrow screen the panel covers the office: it opens on a click.
  if (narrow.matches) {
    $("side").closest(".shell").classList.add("side-collapsed");
    $("side-open").hidden = false;
  }
  // A notification opens /ui/#approvals (summary) or /ui/#task=<id> (that task, J-2).
  const followHash = () => {
    const task = /^#task=([0-9a-f-]{8,64})$/.exec(location.hash);
    if (task) select({ type: "task", id: task[1] });
    else if (location.hash === "#approvals") select(null);
  };
  followHash();
  addEventListener("hashchange", followHash);
  render();
  poll();
  setInterval(() => { if (!document.hidden) poll(); }, POLL_MS);
  loadMetrics();
  setInterval(() => { if (!document.hidden) loadMetrics(); }, METRICS_MS);
}

start();
