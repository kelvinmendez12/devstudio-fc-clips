"""Comprobación e instalación de actualizaciones desde GitHub.

Dos tipos de instalación:
- copia descargada (ZIP): la versión instalada se guarda en `.installed_ref` y se actualiza
  descargando el ZIP de la rama principal;
- clon de git: se actualiza con `git pull --ff-only`.
En ambos casos no se tocan los datos del usuario (viven en su carpeta de datos, fuera de la app).
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import requests

from .config import PROJECT_DIR

log = logging.getLogger(__name__)

REPO = "kelvinmendez12/devstudio-fc-clips"
BRANCH = "main"
CHECK_EVERY_S = 6 * 3600
REF_FILE = ".installed_ref"
# Nunca se sobrescribe al actualizar una copia descargada.
KEEP = {".venv", ".env", ".git", REF_FILE, "__pycache__"}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class UpdateError(Exception):
    pass


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                         creationflags=_NO_WINDOW, timeout=120)
    if out.returncode != 0:
        raise UpdateError(out.stderr.strip() or f"git {' '.join(args)} falló")
    return out.stdout.strip()


def is_git_checkout(root: Path = PROJECT_DIR) -> bool:
    return (root / ".git").exists()


def current_ref(root: Path = PROJECT_DIR) -> str | None:
    ref_file = root / REF_FILE
    if ref_file.exists():
        return ref_file.read_text(encoding="utf-8").strip() or None
    if is_git_checkout(root):
        try:
            return _git(root, "rev-parse", "HEAD")
        except (UpdateError, OSError, subprocess.SubprocessError):
            return None
    return None


def latest_ref(repo: str = REPO, branch: str = BRANCH, timeout: float = 10) -> str:
    resp = requests.get(
        f"https://api.github.com/repos/{repo}/commits/{branch}",
        headers={"Accept": "application/vnd.github.sha", "User-Agent": "devstudio-fc-clips"},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise UpdateError(f"GitHub respondió {resp.status_code}")
    return resp.text.strip()


def check(db, root: Path = PROJECT_DIR, force: bool = False, repo: str = REPO) -> dict:
    """{"available": bool, "current", "latest", "checked_at"}. Consulta GitHub como mucho cada 6 h."""
    cached = db.get_meta("update") or {}
    if not force and cached and time.time() - cached.get("checked_at", 0) < CHECK_EVERY_S:
        cached["available"] = bool(cached.get("latest") and cached["latest"] != current_ref(root))
        return cached
    state = {"checked_at": time.time(), "latest": cached.get("latest"), "error": None}
    try:
        state["latest"] = latest_ref(repo)
    except (requests.RequestException, UpdateError) as exc:
        state["error"] = str(exc)
        log.info("No se pudo comprobar si hay actualizaciones: %s", exc)
    current = current_ref(root)
    state["current"] = current
    state["available"] = bool(state["latest"] and current and state["latest"] != current)
    db.set_meta("update", state)
    return state


def _install_zip(root: Path, repo: str, branch: str, timeout: float = 120) -> str:
    resp = requests.get(f"https://github.com/{repo}/archive/refs/heads/{branch}.zip", timeout=timeout)
    if resp.status_code != 200:
        raise UpdateError(f"No se pudo descargar la actualización ({resp.status_code})")
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            zf.extractall(tmp)
        (src,) = [p for p in Path(tmp).iterdir() if p.is_dir()]  # <repo>-<rama>/
        # Se borra el código anterior (para que desaparezcan los archivos retirados) y se copia el nuevo.
        for item in root.iterdir():
            if item.name in KEEP:
                continue
            shutil.rmtree(item) if item.is_dir() else item.unlink()
        for item in src.iterdir():
            if item.name in KEEP:
                continue
            dst = root / item.name
            shutil.copytree(item, dst) if item.is_dir() else shutil.copy2(item, dst)
    return latest_ref(repo, branch)


def apply_update(root: Path = PROJECT_DIR, repo: str = REPO, branch: str = BRANCH,
                 python: str | None = None) -> str:
    """Instala la última versión y sus dependencias. Devuelve la versión instalada."""
    if is_git_checkout(root):
        if _git(root, "status", "--porcelain", "--untracked-files=no"):
            raise UpdateError("Hay cambios locales sin guardar en la carpeta de la app; no se actualiza")
        _git(root, "fetch", "origin", branch)
        _git(root, "merge", "--ff-only", f"origin/{branch}")
        new_ref = _git(root, "rev-parse", "HEAD")
    else:
        new_ref = _install_zip(root, repo, branch)
        (root / REF_FILE).write_text(new_ref, encoding="utf-8")
    requirements = root / "requirements.txt"
    if requirements.exists():
        pip = subprocess.run([python or sys.executable, "-m", "pip", "install", "--quiet", "-r", str(requirements)],
                             capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=900)
        if pip.returncode != 0:
            raise UpdateError(f"No se pudieron instalar las dependencias: {pip.stderr.strip()[-300:]}")
    return new_ref


# Pequeño programa que espera, y arranca la app de nuevo. Se ejecuta con Python y no con `cmd /c`: la
# orden iba con comillas escapadas con barra (\"), que cmd no entiende, y Windows acababa intentando abrir un
# archivo llamado «\» (rutas con espacios o acentos, como «Méndez», lo empeoraban).
_RELAUNCH = (
    "import subprocess, sys, time; time.sleep(float(sys.argv[1])); "
    "subprocess.Popen(sys.argv[3:], cwd=sys.argv[2], close_fds=True, creationflags=%d)"
)
_DETACHED = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
_NO_WINDOW_FLAGS = 0x00000008 | 0x08000000


def restart_soon(root: Path = PROJECT_DIR, delay_s: int = 4, argv: list[str] | None = None) -> None:
    """Relanza la app tras cerrarse: un proceso aparte espera a que este suelte el bloqueo y arranca otro."""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe") if os.name == "nt" and exe.with_name("pythonw.exe").exists() else exe
    argv = argv or [str(pythonw), "-m", "rkm_clips", "serve"]
    if os.name == "nt":
        subprocess.Popen([str(pythonw), "-c", _RELAUNCH % _DETACHED, str(delay_s), str(root), *argv],
                         creationflags=_NO_WINDOW_FLAGS, close_fds=True)
    else:  # pragma: no cover - la app es para Windows; esto solo ayuda en desarrollo
        subprocess.Popen(["sh", "-c", f'sleep {delay_s}; cd "{root}" && "{exe}" -m rkm_clips serve'],
                         start_new_session=True)
