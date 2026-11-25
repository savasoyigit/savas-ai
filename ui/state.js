// K-3: agent state and room from real backend data only (docs/plan-k.md, karar D).
// Pure functions; app.js fetches, this file decides what the office shows.

export const ROOMS = [
  { id: "obs", name: "Gözlemevi", purpose: "araştırma" },
  { id: "hq", name: "Komuta Merkezi", purpose: "koordinasyon · strateji" },
  { id: "scan", name: "Tarayıcı Oda", purpose: "inceleme · QA" },
  { id: "lab", name: "Laboratuvar", purpose: "kod · geliştirme" },
  { id: "studio", name: "Stüdyo", purpose: "içerik · yazı" },
  { id: "arch", name: "Arşiv", purpose: "Obsidian · hafıza" },
  { id: "rest", name: "Dinlenme Odası", purpose: "boştaki ajanlar" },
];

// worker task records carry no task_type (K-2 note): the room follows the role.
export const ROLE_ROOM = {
  orchestrator: "hq",
  intelligence_strategy: "hq",
  coding_engineering: "lab",
  research_knowledge: "obs",
  content_creativity: "studio",
  qa_review: "scan",
};

export const STATUS_LABEL = {
  idle: "BOŞTA",
  active: "ÇALIŞIYOR",
  thinking: "DÜŞÜNÜYOR",
  waiting: "ONAY BEKLİYOR",
  error: "HATA",
  offline: "ÇEVRİMDIŞI",
  unknown: "BİLİNMİYOR",
};

export const ERROR_WINDOW_MS = 30 * 60 * 1000;
export const FINAL = new Set(["completed", "needs_review", "failed", "blocked", "cancelled", "skipped"]);

const ROLE_LABEL = {
  orchestrator: "Orkestratör",
  intelligence_strategy: "Strateji · istihbarat",
  coding_engineering: "Kod · mühendislik",
  research_knowledge: "Araştırma · bilgi",
  content_creativity: "İçerik · yaratıcılık",
  qa_review: "QA · inceleme",
};

export function roleLabel(role) {
  return ROLE_LABEL[role] || role || "rol bilinmiyor";
}

// Latest failure time per worker from top-level tasks and the cached children.
export function recentFailures(tasks, details, now = Date.now()) {
  const failures = {};
  const note = (worker, task) => {
    if (task.status !== "failed") return;
    const at = Date.parse(task.completed_at || task.updated_at || task.created_at);
    if (Number.isFinite(at) && now - at <= ERROR_WINDOW_MS) {
      failures[worker] = Math.max(failures[worker] || 0, at);
    }
  };
  for (const task of tasks) note(task.worker, task);
  for (const detail of Object.values(details)) {
    for (const child of detail.children || []) note(child.worker, child);
  }
  return failures;
}

function unavailable(health, model) {
  for (const item of health?.unavailable_models || []) {
    if (item === model || item?.model === model || item?.id === model) return true;
  }
  return false;
}

// -> {status, room, task, reason}
export function agentState(worker, { health, workers, failures, connected }) {
  const home = ROLE_ROOM[worker.role] || "hq";
  if (!connected) return { status: "unknown", room: "rest", task: null, reason: "Orchestrator'a ulaşılamıyor." };

  if (health && !(health.routes || {})[worker.id]) {
    return { status: "offline", room: "rest", task: null, reason: "Bu ajan için model rotası yok." };
  }
  if (unavailable(health, worker.model)) {
    return { status: "offline", room: "rest", task: null, reason: `Model kullanılamıyor: ${worker.model}` };
  }

  const tasks = worker.tasks || [];
  const waiting = tasks.find((t) => t.status === "waiting_approval");
  if (waiting) return { status: "waiting", room: home, task: waiting, reason: "Görevi kullanıcı onayı bekliyor." };

  const running = tasks.find((t) => t.status === "running" || t.status === "queued");
  if (running) {
    if (worker.role === "orchestrator") {
      const helpers = workers.filter(
        (w) => w.id !== worker.id && (w.tasks || []).some((t) => t.status === "running" && t.parent_id === running.task_id),
      );
      if (!helpers.length) return { status: "thinking", room: home, task: running, reason: "Görev üzerinde düşünüyor." };
      return { status: "active", room: home, task: running, reason: `Koordine ediyor: ${helpers.map((w) => w.name).join(", ")}` };
    }
    const reason = running.step === "review" ? "İnceleme yapıyor." : running.step === "revision" ? "Düzeltme turunda." : "Görev üzerinde çalışıyor.";
    return { status: "active", room: home, task: running, reason };
  }

  if (failures[worker.id]) {
    return { status: "error", room: home, task: null, reason: `Son görevi başarısız (${new Date(failures[worker.id]).toLocaleTimeString("tr-TR")}).` };
  }
  return { status: "idle", room: "rest", task: null, reason: "Atanmış görev yok." };
}

// Real hand-offs only: a running worker task whose parent is another agent's running task.
export function handoffs(workers) {
  const owner = {};
  for (const w of workers) for (const t of w.tasks || []) if (t.status === "running") owner[t.task_id] = w.id;
  const links = [];
  for (const w of workers) {
    for (const t of w.tasks || []) {
      if (t.status === "running" && t.parent_id && owner[t.parent_id] && owner[t.parent_id] !== w.id) {
        links.push({ from: owner[t.parent_id], to: w.id, step: t.step });
      }
    }
  }
  return links;
}

const NOISE = new Set(["budget_check"]);

export function isNoise(event) {
  return NOISE.has(event.event) || (event.event === "llm_call" && event.status === "started");
}

export function eventKind(e) {
  const s = String(e.status || "");
  if (e.error || ["failed", "error", "invalid", "rejected", "background_error", "timeout"].includes(s)) return "error";
  if (e.event === "runner" && e.status === "test") return e.passed ? "success" : "error";
  if (s === "needs_review") return "warning";
  if (["delegation", "review", "worker_report"].includes(e.event)) return "delegation";
  if (["waiting_approval", "pending", "skipped", "unavailable", "exceeded", "blocked"].includes(s) || e.event === "approval") return "warning";
  if (["completed", "applied", "done", "approved", "executed", "episode_saved"].includes(s)) return "success";
  return "info";
}

const who = (e) => e.worker || e.agent || "sistem";

export function eventText(e) {
  const key = `${e.event}/${e.status}`;
  switch (key) {
    case "task/started": return `Yeni görev alındı: ${clip(e.message, 90)}`;
    case "task/completed": return "Görev tamamlandı.";
    case "task/needs_review": return "Görev bitti ama incelenmeli: inceleyen onaylamadı.";
    case "task/failed": return `Görev başarısız: ${clip(e.error, 90)}`;
    case "task/cancelled": return "Görev iptal edildi.";
    case "delegation/planned": return `Delegasyon planı: ${(e.layers || []).map((l) => l.join(" + ")).join(" → ")}`;
    case "delegation/started": return `${e.worker} görevlendirildi.`;
    case "delegation/completed": return `${e.worker} görevini bitirdi.`;
    case "delegation/skipped": return `${e.worker} atlandı (bağımlı görev başarısız).`;
    case "worker_report/parsed": return `${e.worker} rapor verdi.`;
    case "review/done": return `${e.reviewer} ${e.manual ? "(elle) " : ""}inceledi: ${e.verdict}${e.rounds ? ` · ${e.rounds} düzeltme turu` : e.revised ? " · düzeltme turu yapıldı" : ""}.`;
    case "review/needs_review": return `${e.reviewer} ${e.manual ? "(elle) " : ""}onaylamadı (${e.verdict || "karar yok"}${e.rounds ? `, ${e.rounds} düzeltme turundan sonra` : ""}): incelenmeli.`;
    case "review/started": return `${e.reviewer} incelemeye başladı (elle).`;
    case "code_edit/applied": return `${e.worker}: ${e.blocks} düzenleme bloğu, uygulanan ${(e.applied || []).length}, reddedilen ${(e.rejected || []).length}.`;
    case "code_need/asked": return `${e.worker} şu aralıkları görmek istedi: ${(e.ranges || []).join(", ")}.`;
    case "code_result/ok": return `Kod sonucu (${e.repo}): ${(e.changed || []).join(", ")} · testler ${e.tests} · ${e.reviewer} onayladı.`;
    case "code_result/needs_review": return `Kod sonucu (${e.repo}): ${(e.changed || []).length ? e.changed.join(", ") : "değişiklik yok"} · testler ${e.tests} · ${e.verdict ? `${e.reviewer}: ${e.verdict}` : "inceleme yok"} — incelenmeli.`;
    case "goal/started": return `Hedef başladı: ${e.picks} ilan seçildi.`;
    case "goal/continued": return "Hedef devam ediyor.";
    case "goal/completed": return `Hedef bitti · ${e.used} token.`;
    case "goal/waiting": return `Hedef ilan metni bekliyor · ${e.used} token.`;
    case "goal/stopped": return `Hedef durdu: ${e.error}`;
    case "goal/failed": return `Hedef başarısız: ${e.error}`;
    case "claim_check/unsupported": return `Doğrulanamayan iddia: ${(e.claims || []).join(", ")} — incelenmeli.`;
    case "runner/prepare": return `Sandbox kopyası hazır (${e.files} dosya).`;
    case "runner/test": return e.passed ? `Testler geçti (${Math.round((e.ms || 0) / 100) / 10} sn).` : `Testler geçmedi (çıkış ${e.exit_code}).`;
    case "runner/diff": return `Yama: ${(e.files || []).length} dosya, +${e.added} / −${e.removed}${(e.conflicts || []).length ? ` · ${e.conflicts.length} çakışma` : ""}.`;
    case "memory/retrieved": return `Hafızadan ${(e.facts || []).length} bilgi, ${(e.episodes || []).length} geçmiş görev getirildi.`;
    case "memory/episode_saved": return "Görev hafızaya kaydedildi.";
    case "llm_call/completed": return `Model yanıtı (${e.model || "?"}).`;
    case "chat/started": return "Sohbet mesajı aldı, yanıt yazıyor.";
    case "chat/completed": return `Sohbette yanıt verdi (${e.model || "?"}${e.total_tokens != null ? `, ${e.total_tokens} token` : ""}).`;
    case "chat/failed": return `Sohbet yanıtı başarısız: ${clip(e.error, 90)}`;
    case "chat/blocked": return `Sohbet bütçe sınırına takıldı: ${clip(e.error, 90)}`;
    case "jobs/imported": return `İlan toplama: ${e.new ?? 0} yeni, ${(e.added ?? 0) - (e.new ?? 0)} elenen, ${e.duplicates ?? 0} bilinen.`;
    case "jobs/scored": return `İlan puanladı: ${e.scored ?? 0}/${e.of ?? 0} (${e.model || "?"}${e.total_tokens != null ? `, ${e.total_tokens} token` : ""}).`;
    case "task/proposed": return `Not önerisi onaya gönderildi: ${e.path || "?"}`;
    case "jobs/package_started": return `Başvuru paketi başladı: ${clip(e.title, 60)}`;
    case "jobs/package_ready": return `Başvuru paketi hazır: ${clip(e.title, 60)} (${e.total_tokens ?? "?"} token).`;
    case "jobs/package_needs_fix": return `Başvuru paketi gözden geçirilmeli (Gordon): ${clip(e.title, 60)}`;
    case "jobs/package_failed": return `Başvuru paketi başarısız: ${clip(e.error, 80)}`;
    case "jobs/status_changed": return `İlan durumu: ${clip(e.title, 50)} → ${e.to}`;
  }
  if (e.event === "search") return `Web araması: ${e.status}${e.results != null ? ` (${e.results} sonuç)` : ""}.`;
  if (e.event === "approval") return `Onay ${e.status}: ${e.action || ""}`;
  return `${e.event} · ${e.status}${e.error ? ` — ${clip(e.error, 80)}` : ""}`;
}

export function eventAgent(e) {
  return who(e);
}

export function clip(text, n) {
  const s = String(text ?? "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

export const TASK_STATUS_LABEL = {
  queued: "SIRADA",
  running: "ÇALIŞIYOR",
  waiting_approval: "ONAY BEKLİYOR",
  completed: "TAMAM",
  needs_review: "İNCELENMELİ",
  failed: "BAŞARISIZ",
  blocked: "ENGELLİ",
  cancelled: "İPTAL",
  skipped: "ATLANDI",
};
