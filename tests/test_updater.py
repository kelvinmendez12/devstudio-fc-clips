import io
import os
import time
import zipfile

import pytest

import rkm_clips.updater as updater
from rkm_clips.db import Database


class Resp:
    def __init__(self, status=200, text="", content=b""):
        self.status_code, self.text, self.content = status, text, content


def fake_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(f"devstudio-fc-clips-main/{name}", data)
    return buf.getvalue()


@pytest.fixture
def app_dir(tmp_path):
    root = tmp_path / "app"
    (root / "rkm_clips").mkdir(parents=True)
    (root / "rkm_clips" / "viejo.py").write_text("old")
    (root / "rkm_clips" / "__init__.py").write_text("v1")
    (root / ".venv").mkdir()
    (root / ".venv" / "marca").write_text("no tocar")
    (root / ".env").write_text("LOG_LEVEL=DEBUG")
    (root / updater.REF_FILE).write_text("aaa111")
    return root


def test_check_reports_available_and_caches_for_six_hours(tmp_path, app_dir, monkeypatch):
    db = Database(tmp_path / "u.sqlite3")
    calls = []
    monkeypatch.setattr(updater, "latest_ref", lambda *a, **k: calls.append(1) or "bbb222")
    first = updater.check(db, root=app_dir)
    assert first["available"] and first["latest"] == "bbb222" and first["current"] == "aaa111"
    updater.check(db, root=app_dir)
    assert len(calls) == 1  # la segunda sale de la caché
    updater.check(db, root=app_dir, force=True)
    assert len(calls) == 2


def test_check_survives_github_being_unreachable(tmp_path, app_dir, monkeypatch):
    import requests
    db = Database(tmp_path / "u.sqlite3")

    def boom(*a, **k):
        raise requests.ConnectionError("sin red")

    monkeypatch.setattr(updater, "latest_ref", boom)
    state = updater.check(db, root=app_dir)
    assert state["available"] is False and "sin red" in state["error"]


def test_up_to_date_is_not_reported(tmp_path, app_dir, monkeypatch):
    db = Database(tmp_path / "u.sqlite3")
    monkeypatch.setattr(updater, "latest_ref", lambda *a, **k: "aaa111")
    assert updater.check(db, root=app_dir)["available"] is False


def test_zip_update_replaces_code_but_keeps_user_files(app_dir, monkeypatch):
    zip_bytes = fake_zip({"rkm_clips/__init__.py": "v2", "rkm_clips/nuevo.py": "new", "requirements.txt": ""})
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: Resp(200, content=zip_bytes))
    monkeypatch.setattr(updater, "latest_ref", lambda *a, **k: "bbb222")
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stderr": ""})())
    assert updater.apply_update(root=app_dir) == "bbb222"
    assert (app_dir / "rkm_clips" / "__init__.py").read_text() == "v2"
    assert (app_dir / "rkm_clips" / "nuevo.py").exists()
    assert not (app_dir / "rkm_clips" / "viejo.py").exists()      # lo retirado desaparece
    assert (app_dir / ".venv" / "marca").read_text() == "no tocar"
    assert (app_dir / ".env").read_text() == "LOG_LEVEL=DEBUG"
    assert (app_dir / updater.REF_FILE).read_text() == "bbb222"


def test_failed_download_leaves_the_app_untouched(app_dir, monkeypatch):
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: Resp(404))
    with pytest.raises(updater.UpdateError):
        updater.apply_update(root=app_dir)
    assert (app_dir / "rkm_clips" / "viejo.py").exists()


def test_git_checkout_refuses_to_update_with_local_changes(app_dir, monkeypatch):
    (app_dir / ".git").mkdir()
    (app_dir / updater.REF_FILE).unlink()
    answers = {"status": " M archivo.py"}
    monkeypatch.setattr(updater, "_git", lambda root, *a: answers.get(a[0], ""))
    with pytest.raises(updater.UpdateError, match="cambios locales"):
        updater.apply_update(root=app_dir)


@pytest.mark.skipif(os.name != "nt", reason="el relanzador de Windows")
def test_restart_works_with_spaces_and_accents_in_the_path(tmp_path):
    """Regresión: con rutas con espacios y acentos (Kelvin Méndez) el reinicio no arrancaba y Windows mostraba un error de archivo no encontrado."""
    import sys
    import time
    from rkm_clips import updater
    root = tmp_path / "Kelvin Méndez" / "mi app"
    root.mkdir(parents=True)
    marker = root / "arrancó.txt"
    code = "import os; open('arrancó.txt', 'w', encoding='utf-8').write(os.getcwd())"
    updater.restart_soon(root=root, delay_s=1, argv=[sys.executable, "-c", code])
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(0.1)
    assert marker.exists(), "la app no se relanzó"
    assert marker.read_text(encoding="utf-8").endswith("mi app")     # y lo hizo en la carpeta de la app
