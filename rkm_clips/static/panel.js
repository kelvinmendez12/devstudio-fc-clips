const STATUS = {
  new: ["new", "Sin procesar"],
  queued: ["work", "En cola"],
  downloading: ["work", "Descargando"],
  analyzing: ["work", "Buscando goles"],
  cutting: ["work", "Cortando clips"],
  done: ["done", "Listo"],
  failed: ["failed", "Error"],
  cancelled: ["cancelled", "Cancelado"],
};
const ACTIVE = new Set(["queued", "downloading", "analyzing", "cutting"]);

const ICONS = {
  download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0-4-4m4 4 4-4M5 21h14"/></svg>',
  folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>',
  spark: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l2.2 6.3L20.5 10l-6.3 2.2L12 18.5l-2.2-6.3L3.5 10l6.3-1.7z"/></svg>',
  phone: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="2" width="10" height="20" rx="2.5"/><path d="M11 18h2"/></svg>',
  send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4Z"/></svg>',
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>',
};

let state = null;
const vodsEl = document.getElementById("vods");

function fmtDate(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString("es-ES", { weekday: "short", day: "numeric", month: "short" }) +
    " · " + d.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
}

function ago(ts) {
  if (!ts) return "nunca";
  const min = Math.round((Date.now() / 1000 - ts) / 60);
  return min < 1 ? "hace un momento" : min === 1 ? "hace 1 min" : min < 60 ? `hace ${min} min` : `hace ${Math.round(min / 60)} h`;
}

function renderNotices() {
  const n = [];
  const up = state.update || {};
  const st = up.status || {};
  if (st.state === "running" || st.state === "done") {
    n.push(`<div class="notice">Actualizando la app… se reiniciará sola y esta página se recargará en unos segundos.</div>`);
  } else if (st.state === "error") {
    n.push(`<div class="notice bad">No se pudo actualizar: ${esc(st.error)}
      <button class="btn sm" data-update>Reintentar</button></div>`);
  } else if (up.available) {
    n.push(`<div class="notice">Hay una versión nueva de la app.
      <button class="btn sm primary" data-update>Actualizar ahora</button></div>`);
  }
  const cal = state.calibrated;
  if (!cal.label) {
    n.push(`<div class="notice">Solo falta un paso antes de generar clips: marcar el nombre de tu equipo tal como sale en el marcador (1 minuto).
      <a class="btn sm primary" href="/calibrar">Marcar mi equipo</a></div>`);
  } else if (cal.digits.length === 0) {
    n.push(`<div class="notice">Faltan las plantillas de los números del marcador.
      <a class="btn sm primary" href="/calibrar">Calibrar marcador</a></div>`);
  } else if (cal.digits.length < 10) {
    const missing = [...Array(10).keys()].filter(d => !cal.digits.includes(d));
    n.push(`<div class="notice">Faltan los dígitos ${missing.join(", ")}: si tu equipo llega a esos números no se leerán.
      <a class="btn sm" href="/calibrar">Completar</a></div>`);
  }
  if (state.pending_unknowns) {
    n.push(`<div class="notice">Hay ${state.pending_unknowns} ${state.pending_unknowns === 1 ? "momento" : "momentos"} en que se ve el marcador pero no se reconoce el número de tu equipo: podrían faltar goles.
      <a class="btn sm primary" href="/calibrar#pendientes">Revisar</a></div>`);
  }
  const tt = state.tiktok || {};
  if (tt.configured && !tt.connected) {
    n.push(`<div class="notice">Conecta tu TikTok para que los clips verticales lleguen a tu bandeja como borrador (tú solo pulsas publicar).
      <a class="btn sm primary" href="/auth/tiktok/start">Conectar TikTok</a></div>`);
  }
  if (state.needs_login) {
    n.push(`<div class="notice bad">${esc(state.sync_error)}
      <form method="post" action="/auth/logout" style="margin-left:auto"><button class="btn sm primary" type="submit">Reconectar con Twitch</button></form></div>`);
  } else if (state.sync_error) {
    n.push(`<div class="notice bad">No se pudo consultar Twitch: ${esc(state.sync_error)}</div>`);
  }
  document.getElementById("notices").innerHTML = n.join("");
}

function draftHtml(c) {
  const tt = state.tiktok || {};
  const d = c.draft || {};
  if (d.status === "queued" || d.status === "uploading") {
    return `<span class="tt-working">${ICONS.send} ${d.status === "queued" ? "En cola…" : "Enviando…"}</span>`;
  }
  if (d.status === "sent") {
    return `<span class="tt-sent" title="Ábrelo en la app de TikTok: ahí lo revisas y lo publicas">✓ En tu bandeja de TikTok</span>`;
  }
  if (!tt.connected) return "";
  const err = d.status === "error" ? ` title="${esc(d.error)}"` : "";
  return `<button class="btn sm" data-send="${c.id}"${err}>${ICONS.send} ${d.status === "error" ? "Reintentar envío" : "Enviar a TikTok"}</button>`;
}

function tiktokHtml(c) {
  const t = c.tiktok || {};
  if (t.status === "ready") {
    return `<div class="clip-tt ready">
      <button class="btn sm primary" data-play-tt="${c.id}" data-title="TikTok · ${c.old} → ${c.new}" data-caption="${esc(t.caption)}">${ICONS.phone} Ver TikTok</button>
      <a class="icon-btn" href="/media/tiktok/${c.id}?download=1" title="Descargar versión vertical" aria-label="Descargar versión vertical">${ICONS.download}</a>
      <button class="icon-btn" data-copy="${esc(t.caption)}" title="Copiar el texto del post" aria-label="Copiar el texto del post">${ICONS.copy}</button>
      <button class="icon-btn" data-tiktok="${c.id}" title="Rehacer la versión vertical" aria-label="Rehacer la versión vertical">${ICONS.spark}</button>
      ${draftHtml(c)}
    </div>`;
  }
  if (t.status === "queued" || t.status === "rendering") {
    return `<div class="clip-tt"><span class="tt-working">${ICONS.phone} ${t.status === "queued" ? "En cola…" : "Creando TikTok…"}</span></div>`;
  }
  const err = t.status === "error" ? ` title="${esc(t.error)}"` : "";
  return `<div class="clip-tt"><button class="btn sm" data-tiktok="${c.id}"${err}>${ICONS.phone} ${t.status === "error" ? "Reintentar TikTok" : "Crear TikTok"}</button></div>`;
}

function clipHtml(c) {
  const thumb = c.has_thumb ? `<img src="/media/thumb/${c.id}" alt="" loading="lazy">` : "";
  return `<div class="clip">
    <button class="play" data-play="${c.id}" data-title="${esc(state.team || "Gol")} ${c.old} → ${c.new} · ${esc(c.at)}" aria-label="Reproducir clip del gol ${c.new}">${thumb}</button>
    <div class="clip-body">
      <div>
        <div class="clip-score">${c.old} → <b>${c.new}</b></div>
        <div class="clip-time">${esc(c.at)} · ${c.duration}s</div>
      </div>
      <div class="actions">
        <a class="icon-btn" href="/media/clip/${c.id}?download=1" title="Descargar" aria-label="Descargar">${ICONS.download}</a>
        <button class="icon-btn" data-reveal="${c.id}" title="Mostrar en la carpeta" aria-label="Mostrar en la carpeta">${ICONS.folder}</button>
      </div>
    </div>
    ${tiktokHtml(c)}
  </div>`;
}

function vodHtml(v) {
  const [cls, label] = STATUS[v.status] || ["new", v.status];
  const active = ACTIVE.has(v.status);
  let statusText = label;
  if (active && v.status !== "queued") statusText += ` · ${Math.round(v.progress * 100)}%`;
  if (v.status === "done") statusText += ` · ${v.clips.length} ${v.clips.length === 1 ? "gol" : "goles"}`;
  const warn = v.status === "done" && v.unknowns
    ? `<span class="vod-warn" title="Se vio el marcador con un número sin plantilla">⚠ ${v.unknowns} sin reconocer</span>` : "";

  let action;
  if (v.live) action = `<button class="btn" disabled>En directo…</button>`;
  else if (active) action = `<button class="btn danger" data-cancel="${v.id}">Cancelar</button>`;
  else if (v.status === "done") {
    const pending = v.clips.filter(c => !["ready", "queued", "rendering"].includes((c.tiktok || {}).status)).length;
    const toSend = (state.tiktok || {}).connected
      ? v.clips.filter(c => (c.tiktok || {}).status === "ready" && !["queued", "uploading", "sent"].includes((c.draft || {}).status)).length : 0;
    action = (pending ? `<button class="btn" data-tiktok-vod="${v.id}">${ICONS.phone} TikTok de todos (${pending})</button>` : "") +
      (toSend ? `<button class="btn primary" data-send-vod="${v.id}">${ICONS.send} Enviar a TikTok (${toSend})</button>` : "") +
      `<button class="btn" data-generate="${v.id}">Regenerar</button>`;
  }
  else action = `<button class="btn primary" data-generate="${v.id}">${ICONS.spark} ${v.status === "failed" || v.status === "cancelled" ? "Reintentar" : "Generar clips"}</button>`;

  const progress = active
    ? `<div class="progress ${v.status === "queued" ? "indeterminate" : ""}"><div style="width:${Math.max(2, v.progress * 100)}%"></div></div>`
    : "";

  let clips = "";
  if (v.status === "done") {
    clips = v.clips.length
      ? `<div class="clips">${v.clips.map(clipHtml).join("")}</div>`
      : `<div class="clips-empty">No se detectó ningún gol de tu equipo en este VOD. Si sí hubo goles, revisa la calibración.</div>`;
  }

  return `<article class="vod" id="vod-${v.id}">
    <div class="vod-main">
      <a class="thumb" href="${esc(v.url)}" target="_blank" rel="noopener">
        ${v.thumbnail ? `<img src="${esc(v.thumbnail)}" alt="" loading="lazy">` : ""}
        ${v.live ? '<span class="chip live">En directo</span>' : ""}
        <span class="chip">${esc(v.duration)}</span>
      </a>
      <div class="vod-info">
        <h2 class="vod-title" title="${esc(v.title)}">${esc(v.title) || "Sin título"}</h2>
        <div class="vod-meta"><span>${fmtDate(v.created_at)}</span><span class="status ${cls}">${statusText}</span>${warn}</div>
        ${progress}
        ${v.status === "failed" && v.error ? `<div class="vod-error">${esc(v.error)}</div>` : ""}
      </div>
      <div class="vod-actions">${action}</div>
    </div>
    ${clips}
  </article>`;
}

function render() {
  renderNotices();
  const s = state.sync;
  const tt = state.tiktok || {};
  document.getElementById("sync-line").textContent = state.channel
    ? `twitch.tv/${state.channel} · actualizado ${ago(s && s.synced_at)} · se revisa cada ${state.poll_minutes} min` +
      (tt.connected ? ` · TikTok: ${tt.user} (quedan ${tt.left} de ${tt.limit} borradores hoy)` : "")
    : "Inicia sesión para ver tus VODs";
  document.getElementById("auto-toggle").checked = !!state.auto.enabled;

  if (!state.vods.length) {
    vodsEl.innerHTML = `<div class="empty">Aún no hay VODs. Si acabas de entrar, espera unos segundos o pulsa «Actualizar».</div>`;
    return;
  }
  // Re-render solo si cambió algo, para no reiniciar las miniaturas ni el foco en cada sondeo.
  const html = state.vods.map(vodHtml).join("");
  if (html !== vodsEl.dataset.last) {
    vodsEl.innerHTML = html;
    vodsEl.dataset.last = html;
  }
}

async function refresh() {
  try {
    state = await api("/api/state");
    render();
    if (state.update && state.update.status && state.update.status.state === "done") waitForRestart();
  } catch (e) {
    document.getElementById("sync-line").textContent = e.message;
  }
  const busy = state && state.vods.some(v => ACTIVE.has(v.status) ||
    v.clips.some(c => ["queued", "rendering"].includes((c.tiktok || {}).status) ||
      ["queued", "uploading"].includes((c.draft || {}).status)));
  setTimeout(refresh, busy ? 2000 : 10000);
}

document.getElementById("notices").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-update]");
  if (!btn) return;
  btn.disabled = true;
  try {
    await api("/api/update", { method: "POST" });
    waitForRestart();
  } catch (e) {
    btn.disabled = false;
    toast(e.message, true);
  }
});

// Tras actualizar, el servidor se cierra y vuelve a arrancar: se espera a que responda y se recarga.
function waitForRestart() {
  let down = false;
  const timer = setInterval(async () => {
    try {
      const r = await fetch("/api/state", { cache: "no-store" });
      if (down && r.status < 500) { clearInterval(timer); location.reload(); }
    } catch { down = true; }
  }, 2500);
}

vodsEl.addEventListener("click", async (ev) => {
  const el = ev.target.closest("[data-generate],[data-cancel],[data-play],[data-reveal],[data-tiktok],[data-tiktok-vod],[data-send],[data-send-vod],[data-play-tt],[data-copy]");
  if (!el) return;
  try {
    if (el.dataset.tiktok) {
      el.disabled = true;
      await api(`/api/clips/${el.dataset.tiktok}/tiktok`, { method: "POST" });
    } else if (el.dataset.send) {
      el.disabled = true;
      await api(`/api/clips/${el.dataset.send}/tiktok/send`, { method: "POST" });
    } else if (el.dataset.sendVod) {
      el.disabled = true;
      const r = await api(`/api/vods/${el.dataset.sendVod}/tiktok/send`, { method: "POST" });
      toast(r.remaining ? `Enviando ${r.queued}; ${r.remaining} quedan para mañana (TikTok admite 5 borradores al día)`
                        : `Enviando ${r.queued} a tu TikTok…`);
    } else if (el.dataset.tiktokVod) {
      el.disabled = true;
      const r = await api(`/api/vods/${el.dataset.tiktokVod}/tiktok`, { method: "POST" });
      toast(`Creando ${r.queued} versiones para TikTok…`);
    } else if (el.dataset.playTt) {
      openPlayer(el.dataset.playTt, el.dataset.title, el.dataset.caption);
      return;
    } else if (el.dataset.copy !== undefined) {
      await copyText(el.dataset.copy);
      return;
    } else if (el.dataset.generate) {
      el.disabled = true;
      await api(`/api/vods/${el.dataset.generate}/generate`, { method: "POST" });
      toast("En cola. Puedes cerrar esta pestaña: seguirá trabajando.");
    } else if (el.dataset.cancel) {
      el.disabled = true;
      await api(`/api/vods/${el.dataset.cancel}/cancel`, { method: "POST" });
    } else if (el.dataset.play) {
      openPlayer(el.dataset.play, el.dataset.title);
      return;
    } else if (el.dataset.reveal) {
      await api(`/api/clips/${el.dataset.reveal}/reveal`, { method: "POST" });
      return;
    }
    state = await api("/api/state");
    render();
  } catch (e) {
    el.disabled = false;
    toast(e.message, true);
  }
});

const player = document.getElementById("player");
const video = document.getElementById("player-video");
const playerCopy = document.getElementById("player-copy");
let playerCaption = "";

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Texto copiado: pégalo en tu post de TikTok");
  } catch {
    toast("No se pudo copiar automáticamente", true);
  }
}

// caption presente = versión vertical de TikTok (/media/tiktok); si no, el clip normal.
function openPlayer(id, title, caption) {
  const vertical = caption !== undefined;
  const base = vertical ? "/media/tiktok" : "/media/clip";
  player.classList.toggle("vertical", vertical);
  video.src = `${base}/${id}`;
  document.getElementById("player-title").textContent = title;
  document.getElementById("player-download").href = `${base}/${id}?download=1`;
  playerCaption = caption || "";
  playerCopy.hidden = !vertical;
  player.showModal();
  video.play().catch(() => {});
}
playerCopy.addEventListener("click", () => copyText(playerCaption));
player.addEventListener("close", () => { video.pause(); video.removeAttribute("src"); video.load(); });
player.addEventListener("click", (e) => { if (e.target === player) player.close(); });
document.getElementById("player-close").addEventListener("click", () => player.close());

document.getElementById("btn-sync").addEventListener("click", async () => {
  await api("/api/sync", { method: "POST" });
  toast("Consultando Twitch…");
  setTimeout(async () => { state = await api("/api/state"); render(); }, 2500);
});
document.getElementById("btn-folder").addEventListener("click", () => api("/api/open-clips-folder", { method: "POST" }));

// Vuelta del login de TikTok: el servidor deja el resultado en la URL.
(function () {
  const q = new URLSearchParams(location.search);
  if (q.has("tiktok") || q.has("tiktok_error")) {
    if (q.get("tiktok_error")) toast("TikTok: " + q.get("tiktok_error"), true);
    else toast("TikTok conectado");
    history.replaceState(null, "", "/");
  }
})();

refresh();
