// Pantalla «Identidad del club»: logo, color y vista previa en vivo del marcador.

const $ = (id) => document.getElementById(id);
const preview = $("preview");
let club = null;
let score = [0, 1];
let previewTimer, saveTimer, previewSeq = 0;

function previewUrl() {
  const q = new URLSearchParams({
    primary: $("hex").value.trim(), show_name: $("show-name").checked ? "1" : "0",
    short_name: $("short-name").value, old: score[0], new: score[1],
    replay: $("replay").checked ? "1" : "0", w: 540, _: ++previewSeq,
  });
  return `/media/club/preview.png?${q}`;
}

// Se carga en una imagen aparte y se cambia al terminar: así no parpadea al ir probando colores.
function refreshPreview(delay = 120) {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => {
    const seq = previewSeq + 1;
    const probe = new Image();
    probe.onload = () => {
      if (seq >= previewSeq) preview.src = probe.src;
      preview.alt = `Vista previa: marcador del club con el resultado ${score[0]} → ${score[1]}`;
    };
    probe.onerror = () => toast("No se pudo crear la vista previa", true);
    probe.src = previewUrl();
  }, delay);
}

function render() {
  const c = club;
  $("hex").value = c.primary;
  $("color").value = c.primary;
  $("show-name").checked = c.show_name;
  $("short-name").value = c.short_name;
  $("team-hint").textContent = c.team
    ? `Si lo dejas vacío se usa «${c.team}», el nombre de tu equipo del marcador.`
    : "Si lo dejas vacío se usa el nombre de tu equipo (se escribe en «Calibrar marcador»).";
  const thumb = $("logo-thumb");
  thumb.hidden = !c.logo;
  if (c.logo) thumb.src = c.logo.url;
  $("drop-text").innerHTML = c.logo
    ? `<b>Logo cargado</b> (${c.logo.width}×${c.logo.height}). Pulsa o arrastra otro para cambiarlo`
    : "<b>Arrastra tu logo aquí</b> o pulsa para elegirlo";
  $("logo-delete").hidden = !c.logo;
  const sw = $("swatches");
  sw.innerHTML = (c.suggested || []).map(h =>
    `<button type="button" class="swatch" style="background:${esc(h)}" data-color="${esc(h)}" title="Usar ${esc(h)}" aria-label="Usar el color ${esc(h)} de tu logo"></button>`).join("");
  $("color-help").textContent = c.suggested && c.suggested.length
    ? "Colores sacados de tu logo (pulsa uno), o elige el que quieras."
    : (c.logo ? "Tu logo no tiene un color claro: elige uno." : "Se saca solo de tu logo cuando lo subas, pero puedes cambiarlo.");
}

async function load() {
  club = await api("/api/club");
  render();
  refreshPreview(0);
}

function saveSoon(changes) {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      club = await api("/api/club", { body: changes });
      toast("Guardado");
    } catch (e) {
      toast(e.message, true);
    }
  }, 350);
}

function setColor(hex, save = true) {
  $("hex").value = hex;
  if (/^#[0-9a-f]{6}$/i.test(hex)) {
    $("color").value = hex;
    refreshPreview();
    if (save) saveSoon({ primary: hex });
  }
}

$("color").addEventListener("input", (e) => setColor(e.target.value.toUpperCase()));
$("hex").addEventListener("input", (e) => {
  let v = e.target.value.trim();
  if (v && v[0] !== "#") v = "#" + v;
  setColor(v.toUpperCase());
});
$("swatches").addEventListener("click", (e) => {
  const b = e.target.closest("[data-color]");
  if (b) setColor(b.dataset.color);
});
$("show-name").addEventListener("change", () => { refreshPreview(0); saveSoon({ show_name: $("show-name").checked }); });
$("short-name").addEventListener("input", () => { refreshPreview(); saveSoon({ short_name: $("short-name").value }); });
$("replay").addEventListener("change", () => refreshPreview(0));
document.querySelector(".seg").addEventListener("click", (e) => {
  const b = e.target.closest("[data-score]");
  if (!b) return;
  score = b.dataset.score.split(",").map(Number);
  document.querySelectorAll("[data-score]").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
  refreshPreview(0);
});

// --- subida del logo -------------------------------------------------------------------------------
async function upload(file) {
  if (!file) return;
  const form = new FormData();
  form.append("logo", file);
  form.append("remove_bg", $("remove-bg").checked ? "1" : "0");
  $("drop").classList.add("busy");
  try {
    // api() manda JSON; la subida va como multipart y el navegador pone la cabecera sola
    const res = await fetch("/api/club/logo", { method: "POST", body: form });
    if (res.status === 401) { location.href = "/"; return; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Error ${res.status}`);
    club = data;
    render();
    refreshPreview(0);
    toast("Logo guardado");
  } catch (e) {
    toast(e.message, true);
  } finally {
    $("drop").classList.remove("busy");
    $("file").value = "";
  }
}

const drop = $("drop");
drop.addEventListener("click", () => $("file").click());
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("file").click(); } });
$("file").addEventListener("change", (e) => upload(e.target.files[0]));
["dragenter", "dragover"].forEach(t => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(t => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => upload(e.dataTransfer.files[0]));

$("logo-delete").addEventListener("click", async () => {
  if (!confirm("¿Quitar el logo? El marcador usará un escudo con las iniciales de tu club.")) return;
  try {
    club = await api("/api/club/logo/delete", { method: "POST" });
    render();
    refreshPreview(0);
  } catch (e) {
    toast(e.message, true);
  }
});

load().catch((e) => toast(e.message, true));
