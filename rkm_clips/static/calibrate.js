// Calibración: el fotograma está a resolución de referencia (p. ej. 1920x1080) y todas las
// coordenadas que se guardan están en ese espacio, no en el de la pantalla.

const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
const img = new Image();
let cal = null;            // estado de /api/calibrate
let view = null;           // zona del fotograma que se ve: {x, y, w, h}
let box = null;            // recuadro dibujado, en coordenadas del fotograma
let drag = null;
let selectedDigit = null;

const $ = (id) => document.getElementById(id);
const mode = () => document.querySelector("input[name=mode]:checked").value;

function parseTime(text) {
  return text.trim().split(":").reduce((acc, p) => acc * 60 + Number(p || 0), 0);
}

function draw() {
  if (!img.complete || !img.naturalWidth || !view) return;
  canvas.width = Math.round(Math.min(1600, view.w * Math.max(1, 1000 / view.w)));
  canvas.height = Math.round(canvas.width * view.h / view.w);
  const k = canvas.width / view.w;
  ctx.imageSmoothingEnabled = view.w > 800;  // ampliado: píxeles nítidos para recortar fino
  ctx.drawImage(img, view.x, view.y, view.w, view.h, 0, 0, canvas.width, canvas.height);

  const rect = (b, color, dash = []) => {
    ctx.save();
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.setLineDash(dash);
    ctx.strokeRect((b[0] - view.x) * k, (b[1] - view.y) * k, b[2] * k, b[3] * k);
    ctx.restore();
  };
  if (cal && cal.roi) rect(cal.roi, "#9146ff", [6, 4]);
  if (box) {
    ctx.fillStyle = "rgba(255, 181, 71, .15)";
    ctx.fillRect((box[0] - view.x) * k, (box[1] - view.y) * k, box[2] * k, box[3] * k);
    rect(box, "#ffb547");
  }
}

function toFrame(ev) {
  const r = canvas.getBoundingClientRect();
  return [
    view.x + (ev.clientX - r.left) / r.width * view.w,
    view.y + (ev.clientY - r.top) / r.height * view.h,
  ];
}

canvas.addEventListener("pointerdown", (ev) => {
  canvas.setPointerCapture(ev.pointerId);
  drag = toFrame(ev);
  box = null;
});
canvas.addEventListener("pointermove", (ev) => {
  if (!drag) return;
  const [x, y] = toFrame(ev);
  box = [Math.min(x, drag[0]), Math.min(y, drag[1]), Math.abs(x - drag[0]), Math.abs(y - drag[1])].map(Math.round);
  draw();
});
canvas.addEventListener("pointerup", () => {
  drag = null;
  $("btn-save").disabled = !box || box[2] < 3 || box[3] < 3;
  if (box) $("stage-hint").textContent = `Recuadro ${box[2]}×${box[3]} px en (${box[0]}, ${box[1]}). Pulsa «Guardar recuadro».`;
});

function setView(zoom) {
  const [W, H] = cal.ref;
  if (zoom && cal.roi) {
    const [x, y, w, h] = cal.roi;
    view = { x, y, w, h };
  } else {
    view = { x: 0, y: 0, w: W, h: H };
  }
  $("btn-zoom").textContent = zoom ? "Ver fotograma completo" : "Ampliar marcador";
  $("btn-zoom").dataset.zoom = zoom ? "1" : "";
  draw();
}

function loadFrame() {
  img.onload = () => {
    $("placeholder").hidden = true;
    canvas.hidden = false;
    $("btn-zoom").disabled = false;
    setView(!!$("btn-zoom").dataset.zoom);
  };
  img.src = `/media/calibration-frame?t=${Date.now()}`;
}

function renderCal() {
  $("done-roi").textContent = cal.roi_saved ? "✓" : "";
  $("done-label").textContent = cal.label ? "✓" : "";
  $("done-number").textContent = cal.number_box ? "✓" : "";
  $("digits").innerHTML = [...Array(10).keys()].map(d =>
    `<button class="digit-chip ${cal.digits.includes(d) ? "have" : ""} ${selectedDigit === d ? "sel" : ""}" data-digit="${d}" title="${cal.digits.includes(d) ? "Ya guardado (puedes reemplazarlo)" : "Falta"}">${d}</button>`
  ).join("");
  const t = Date.now();
  $("tpl-preview").innerHTML =
    (cal.label ? `<img src="/media/template/label?t=${t}" alt="Equipo" title="Nombre del equipo">` : "") +
    cal.digits.map(d => `<img src="/media/template/${d}?t=${t}" alt="${d}" title="Dígito ${d}">`).join("");
  if (cal.frame_info) $("frame-info").textContent = `Fotograma actual: VOD ${cal.frame_info.vod_id} en ${cal.frame_info.at}`;
}

$("digits").addEventListener("click", (ev) => {
  const chip = ev.target.closest("[data-digit]");
  if (!chip) return;
  selectedDigit = Number(chip.dataset.digit);
  document.querySelector("input[name=mode][value=digit]").checked = true;
  renderCal();
});

$("btn-frame").addEventListener("click", async () => {
  const btn = $("btn-frame");
  btn.disabled = true;
  btn.textContent = "Cargando…";
  try {
    cal = await api("/api/calibrate/frame", { body: { vod_id: $("vod").value, at: parseTime($("at").value) } });
    renderCal();
    loadFrame();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Cargar fotograma";
  }
});

$("btn-zoom").addEventListener("click", () => { box = null; setView(!$("btn-zoom").dataset.zoom); });

$("btn-save").addEventListener("click", async () => {
  const kind = mode();
  if (kind === "digit" && selectedDigit === null) { toast("Elige abajo qué dígito es", true); return; }
  try {
    cal = await api("/api/calibrate/save", { body: { kind, box, digit: selectedDigit } });
    const names = { roi: "Zona del marcador", label: "Nombre del equipo", number: "Zona del número", digit: `Dígito ${selectedDigit}` };
    toast(`${names[kind]} guardado`);
    box = null;
    $("btn-save").disabled = true;
    if (kind === "roi") setView(true);  // tras marcar el marcador, ampliar para lo fino
    renderCal();
    draw();
  } catch (e) {
    toast(e.message, true);
  }
});

$("btn-test").addEventListener("click", async () => {
  const out = $("test-result");
  try {
    const r = await api("/api/calibrate/test", { method: "POST" });
    out.hidden = false;
    out.innerHTML = r.label_ok
      ? `<div class="big">${r.value ?? "—"}</div>Goles de tu equipo leídos${r.value === null ? " (no se reconoció ningún dígito: guarda el dígito que se ve)" : ""}.<br><small style="color:var(--muted)">Coincidencia del nombre del equipo: ${Math.round(r.label_score * 100)}%</small>`
      : `No encuentro el nombre de tu equipo en la zona del marcador (coincidencia ${Math.round(r.label_score * 100)}%). Revisa la zona del marcador o vuelve a marcar el nombre.`;
  } catch (e) {
    toast(e.message, true);
  }
});

// --- números sin reconocer -------------------------------------------------
let pending = [];
let activeUnknown = null;

function renderPending() {
  $("pendientes").hidden = pending.length === 0;
  $("pending-count").textContent = pending.length;
  $("pending-list").innerHTML = pending.map(u => `
    <div class="pending-item ${u.id === activeUnknown ? "active" : ""}">
      <span class="ts">${esc(u.at)}</span><span class="t" title="${esc(u.title)}">${esc(u.title)}</span>
      <button class="btn sm" data-unknown="${u.id}">Ver</button>
    </div>`).join("");
  $("pending-answer").hidden = activeUnknown === null;
  $("answer-digits").innerHTML = [...Array(10).keys()]
    .map(d => `<button class="digit-chip" data-answer="${d}">${d}</button>`).join("");
}

async function refreshPending() {
  pending = await api("/api/unknowns");
  if (!pending.some(u => u.id === activeUnknown)) activeUnknown = null;
  renderPending();
}

$("pending-list").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-unknown]");
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = "Cargando…";
  try {
    cal = await api(`/api/unknowns/${btn.dataset.unknown}/load`, { method: "POST" });
    activeUnknown = Number(btn.dataset.unknown);
    $("btn-zoom").dataset.zoom = "1";
    renderCal();
    loadFrame();
    renderPending();
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
    btn.textContent = "Ver";
  }
});

$("answer-digits").addEventListener("click", async (ev) => {
  const chip = ev.target.closest("[data-answer]");
  if (!chip || activeUnknown === null) return;
  try {
    const r = await api(`/api/unknowns/${activeUnknown}/resolve`, { body: { digit: Number(chip.dataset.answer) } });
    toast(r.value === Number(chip.dataset.answer)
      ? `Dígito ${chip.dataset.answer} guardado y comprobado. Regenera ese VOD para recuperar los goles.`
      : `Dígito ${chip.dataset.answer} guardado, pero la lectura de comprobación dio ${r.value}. Revisa la zona del número.`,
      r.value !== Number(chip.dataset.answer));
    cal = await api("/api/calibrate");
    activeUnknown = null;
    renderCal();
    await refreshPending();
  } catch (e) {
    toast(e.message, true);
  }
});

$("btn-dismiss").addEventListener("click", async () => {
  if (activeUnknown === null) return;
  await api(`/api/unknowns/${activeUnknown}/dismiss`, { method: "POST" });
  activeUnknown = null;
  await refreshPending();
});

$("team").addEventListener("change", async () => {
  try {
    const r = await api("/api/team", { body: { name: $("team").value } });
    $("team").value = r.team;
    toast("Nombre del equipo guardado");
  } catch (e) { toast(e.message, true); }
});

(async function init() {
  const [state, c] = await Promise.all([api("/api/state"), api("/api/calibrate")]);
  cal = c;
  refreshPending().catch(e => toast(e.message, true));
  $("vod").innerHTML = state.vods.length
    ? state.vods.map(v => `<option value="${v.id}">${esc(v.title || v.id)} · ${new Date(v.created_at).toLocaleDateString("es-ES")}</option>`).join("")
    : `<option value="">No hay VODs todavía</option>`;
  if (cal.frame_info) $("vod").value = cal.frame_info.vod_id;
  $("team").value = cal.team || "";
  renderCal();
  if (cal.roi_saved) $("btn-zoom").dataset.zoom = "1";  // ya calibrado: abrir ampliado
  if (cal.has_frame) loadFrame();
})().catch(e => toast(e.message, true));

window.addEventListener("resize", draw);
