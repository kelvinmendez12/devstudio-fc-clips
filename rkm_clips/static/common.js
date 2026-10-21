// Utilidades compartidas por el panel y la calibración.

async function api(path, options = {}) {
  const res = await fetch(path, {
    method: options.body !== undefined || options.method === "POST" ? "POST" : "GET",
    headers: { "Content-Type": "application/json" },
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });
  if (res.status === 401) { location.href = "/"; throw new Error("Sesión caducada"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Error ${res.status}`);
  return data;
}

let toastTimer;
function toast(message, bad = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.toggle("bad", bad);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3500);
}

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Interruptor de modo automático (en la barra superior de todas las páginas).
(function setupAuto() {
  const toggle = document.getElementById("auto-toggle");
  if (!toggle) return;
  api("/api/state").then(s => { toggle.checked = !!s.auto.enabled; }).catch(() => {});
  toggle.addEventListener("change", async () => {
    try {
      await api("/api/auto", { body: { enabled: toggle.checked } });
      toast(toggle.checked
        ? "Automático activado: los VODs nuevos se procesarán solos"
        : "Automático desactivado");
    } catch (e) {
      toggle.checked = !toggle.checked;
      toast(e.message, true);
    }
  });
})();
