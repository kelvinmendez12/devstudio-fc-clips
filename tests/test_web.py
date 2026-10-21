import numpy as np
import pytest

from conftest import scoreboard
from rkm_clips.config import load_config
from rkm_clips.db import Database
from rkm_clips.detector import imwrite
from rkm_clips.web import create_app, trim_glyph
from test_pipeline import FakeTwitch, vod
from rkm_clips.pipeline import sync_vods


class FakeWorker:
    last_sync_error = None
    needs_login = False

    def __init__(self):
        self.queued = []

    def enqueue(self, db, vod_id):
        self.queued.append(vod_id)
        db.set_status(vod_id, "queued")
        return True

    def cancel(self, db, vod_id):
        return True

    def sync_now(self):
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("TWITCH_CHANNEL", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLIPS_DIR", str(tmp_path / "clips"))
    monkeypatch.setenv("TEMPLATES_DIR", str(tmp_path / "plantillas"))
    cfg = load_config(env_file=tmp_path / "no.env")
    worker = FakeWorker()
    app = create_app(cfg, worker)
    db = Database(cfg.db_path)
    db.set_meta("user", {"id": "42", "login": "kangrykelvin", "display_name": "KangryKelvin", "profile_image_url": ""})
    sync_vods(cfg, db, FakeTwitch([vod(1)]), "kangrykelvin")
    client = app.test_client()
    return cfg, app, client, worker, db


def login(client):
    with client.session_transaction() as s:
        s["uid"] = "42"


def test_requires_login(setup):
    _, _, client, _, _ = setup
    assert b"Conectar con Twitch" in client.get("http://localhost/").data
    assert client.get("http://localhost/api/state").status_code == 401


def test_redirects_127_to_localhost(setup):
    _, _, client, _, _ = setup
    resp = client.get("http://127.0.0.1:8765/")
    assert resp.status_code == 302 and resp.location.startswith("http://localhost")


def test_device_poll_without_start_is_rejected(setup):
    _, _, client, _, _ = setup
    resp = client.post("http://localhost/auth/device/poll")
    assert resp.status_code == 410 and resp.get_json()["status"] == "expired"


def test_state_and_generate_requires_calibration(setup):
    cfg, _, client, worker, db = setup
    login(client)
    state = client.get("http://localhost/api/state").get_json()
    assert state["channel"] == "kangrykelvin"
    assert state["vods"][0]["thumbnail"] == "https://x/640x360.jpg"
    resp = client.post("http://localhost/api/vods/1/generate")
    assert resp.status_code == 409 and "calibrar" in resp.get_json()["error"]
    assert worker.queued == []


def test_calibration_flow(setup, templates):
    cfg, _, client, worker, db = setup
    login(client)
    # Fotograma de referencia 1920x1080 con el marcador sintético arriba a la izquierda.
    frame = np.full((1080, 1920), 40, np.uint8)
    frame[20:260, 30:390] = scoreboard(12, templates)
    imwrite(cfg.data_dir / "calibration" / "frame.png", frame)

    def save(kind, box, **extra):
        return client.post("http://localhost/api/calibrate/save", json={"kind": kind, "box": box, **extra})

    lh, lw = templates.label.shape
    assert save("roi", [0, 0, 480, 300]).status_code == 200
    # Recuadros algo holgados: trim_glyph los ajusta al texto.
    assert save("label", [30 + 20 - 3, 20 + 110 - 3, lw + 6, lh + 6]).status_code == 200
    x = 30 + 20 + lw + 18
    d1w, d2w = templates.digits[1].shape[1], templates.digits[2].shape[1]
    assert save("digit", [x - 1, 130 - 1, d1w + 2, lh + 2], digit=1).status_code == 200
    assert save("digit", [x + d1w + 3 - 1, 130 - 1, d2w + 2, lh + 2], digit=2).status_code == 200
    resp = save("number", [x - 4, 130 - 6, 80, lh + 12])
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["digits"] == [1, 2]

    result = client.post("http://localhost/api/calibrate/test").get_json()
    assert result["label_ok"] and result["value"] == 12

    assert client.post("http://localhost/api/vods/1/generate").status_code == 200
    assert worker.queued == ["1"]


def test_trim_glyph_handles_dark_on_light():
    img = np.full((30, 30), 230, np.uint8)
    img[10:20, 12:16] = 20
    assert trim_glyph(img).shape == (12, 6)


def test_listen_addresses_cover_ipv4_and_ipv6_localhost():
    from rkm_clips.web import listen_addresses
    listen = listen_addresses("127.0.0.1", 8765)
    assert listen.startswith("127.0.0.1:8765")
    assert listen_addresses("0.0.0.0", 9000) == "0.0.0.0:9000"


def test_loading_a_new_frame_keeps_the_calibration(setup, templates, monkeypatch):
    cfg, _, client, _, _ = setup
    login(client)
    import rkm_clips.web as web
    frame = np.full((1080, 1920), 40, np.uint8)
    monkeypatch.setattr(web, "stream_url", lambda url, fmt: "x")
    monkeypatch.setattr(web.ffmpeg, "grab_frame",
                        lambda *a, **k: imwrite(cfg.data_dir / "calibration" / "frame.png", frame))
    client.post("http://localhost/api/calibrate/save", json={"kind": "roi", "box": [10, 20, 480, 300]})
    state = client.post("http://localhost/api/calibrate/frame", json={"vod_id": "1", "at": 960}).get_json()
    assert state["roi_saved"] and state["roi"] == [10, 20, 480, 300]
    assert state["frame_info"] == {"vod_id": "1", "at": "00:16:00"}


def test_session_cookie_has_its_own_name(setup):
    _, app, _, _, _ = setup
    assert app.config["SESSION_COOKIE_NAME"] == "fc_clips_session"


def test_resolving_an_unknown_creates_the_digit_template(setup, templates, monkeypatch):
    cfg, _, client, _, db = setup
    login(client)
    import rkm_clips.web as web
    lh, lw = templates.label.shape
    # Plantillas: etiqueta + dígitos salvo el 7; zona del número calibrada.
    imwrite(cfg.templates_dir / "rkm_label.png", templates.label)
    for d, t in templates.digits.items():
        if d != 7:
            imwrite(cfg.templates_dir / "digits" / f"{d}.png", t)
    frame = np.full((1080, 1920), 40, np.uint8)
    frame[20:260, 30:390] = scoreboard(7, templates)
    monkeypatch.setattr(web, "stream_url", lambda url, fmt: "x")
    monkeypatch.setattr(web.ffmpeg, "grab_frame",
                        lambda *a, **k: imwrite(cfg.data_dir / "calibration" / "frame.png", frame))
    client.post("http://localhost/api/calibrate/save", json={"kind": "roi", "box": [0, 0, 480, 300]})
    db.set_meta("calib_number_box", [lw + 18 - 4, -6, 60, lh + 12])

    db.add_unknown("1", 120.0)
    pend = client.get("http://localhost/api/unknowns").get_json()
    assert len(pend) == 1 and pend[0]["at"] == "00:02:00"
    uid = pend[0]["id"]
    # Sin cargar antes su fotograma no se puede resolver.
    assert client.post(f"http://localhost/api/unknowns/{uid}/resolve", json={"digit": 7}).status_code == 400
    assert client.post(f"http://localhost/api/unknowns/{uid}/load").status_code == 200
    r = client.post(f"http://localhost/api/unknowns/{uid}/resolve", json={"digit": 7}).get_json()
    assert r["ok"] and r["value"] == 7 and 7 in r["digits"]
    assert client.get("http://localhost/api/unknowns").get_json() == []
    assert client.get("http://localhost/api/state").get_json()["pending_unknowns"] == 0


def test_trim_glyph_ignores_box_edge_and_specks():
    # Dígito oscuro sobre blanco, con el filo gris del recuadro a la derecha y motas sueltas.
    img = np.full((29, 40), 240, np.uint8)
    img[6:24, 12:28] = 20          # el "dígito" (18x16)
    img[:, 38:40] = 150            # filo del recuadro pegado al borde
    img[1, 3] = img[2, 33] = 60    # motas
    assert trim_glyph(img).shape == (20, 18)


class FakeDeviceTwitch:
    """Sustituye a TwitchClient en las rutas de login: primero 'pendiente', luego autorizado."""
    polls = 0

    def __init__(self, client_id, token_store=None, **kw):
        self.store = token_store

    def start_device_login(self):
        from rkm_clips.twitch import DeviceCode
        return DeviceCode("dev-code", "ABCD1234", "https://www.twitch.tv/activate?device-code=ABCD1234", 1800, 5)

    def poll_device_login(self, device_code):
        from rkm_clips.twitch import TwitchUser
        assert device_code == "dev-code"
        FakeDeviceTwitch.polls += 1
        if FakeDeviceTwitch.polls < 2:
            return None
        self.store.save_user_token("acc", "ref", 9e12)
        return TwitchUser("77", "otro_club", "Otro Club", "")


def test_device_login_flow_connects_account(setup, monkeypatch):
    cfg, app, client, worker, db = setup
    import rkm_clips.web as web
    db.clear_user_token()  # instalación nueva: nadie conectado
    monkeypatch.setattr(web, "TwitchClient", FakeDeviceTwitch)
    FakeDeviceTwitch.polls = 0

    assert client.get("http://localhost/api/state").status_code == 401
    start = client.post("http://localhost/auth/device/start").get_json()
    assert start["user_code"] == "ABCD1234" and "twitch.tv/activate" in start["verification_uri"]
    assert client.post("http://localhost/auth/device/poll").get_json() == {"status": "pending"}
    done = client.post("http://localhost/auth/device/poll").get_json()
    assert done == {"status": "ok", "login": "otro_club"}

    state = client.get("http://localhost/api/state").get_json()
    assert state["channel"] == "otro_club"
    assert db.get_user_token()["refresh"] == "ref"


def test_logout_forgets_the_twitch_account(setup):
    cfg, app, client, worker, db = setup
    login(client)
    db.save_user_token("acc", "ref", 9e12)
    client.post("http://localhost/auth/logout")
    assert db.get_user_token() is None and db.get_meta("user") is None
    assert client.get("http://localhost/api/state").status_code == 401


def test_team_name_is_saved_and_reported(setup):
    _, _, client, _, _ = setup
    login(client)
    assert client.post("http://localhost/api/team", json={"name": "  Mi Club  "}).get_json() == {"team": "Mi Club"}
    assert client.get("http://localhost/api/state").get_json()["team"] == "Mi Club"
    assert client.get("http://localhost/api/calibrate").get_json()["team"] == "Mi Club"


def test_update_is_refused_while_a_vod_is_processing(setup):
    cfg, _, client, worker, db = setup
    login(client)
    db.start_attempt("1")  # un VOD en descarga
    resp = client.post("http://localhost/api/update")
    assert resp.status_code == 409 and "proceso" in resp.get_json()["error"]


def test_state_reports_update_availability(setup):
    cfg, _, client, worker, db = setup
    login(client)
    db.set_meta("update", {"available": True, "latest": "abc", "checked_at": 1})
    state = client.get("http://localhost/api/state").get_json()
    assert state["update"]["available"] is True
