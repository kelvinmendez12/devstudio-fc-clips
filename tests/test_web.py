import time
from urllib.parse import unquote

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
        self.renders = []
        self.sends = []

    def enqueue_send(self, db, clip_id):
        self.sends.append(clip_id)
        db.set_draft(clip_id, "queued")
        return True

    def enqueue_sends_for_vod(self, db, vod_id):
        from rkm_clips import tiktok
        left = tiktok.drafts_left(db)
        return sum(self.enqueue_send(db, c["id"]) for c in db.clips_ready_for_draft(vod_id)[:left])

    def enqueue_render(self, db, clip_id):
        self.renders.append(clip_id)
        db.set_tiktok(clip_id, "queued")
        return True

    def enqueue_renders_for_vod(self, db, vod_id):
        return sum(self.enqueue_render(db, c["id"]) for c in db.clips_missing_tiktok(vod_id))

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
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "cs")
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


def add_clip(cfg, db, vod_id="1", new=2, exists=True):
    from rkm_clips.detector import Change
    cfg.clips_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.clips_dir / f"clip{new}.mp4"
    if exists:
        path.write_bytes(b"clip-original")
    db.add_clip(vod_id, Change(at_s=95.0, old=new - 1, new=new, last_old_s=94.0), 79.0, 97.0, path, None)
    return db.clips_for(vod_id)[-1]["id"], path


def test_clip_json_reports_tiktok_state_and_caption(setup):
    cfg, _, client, worker, db = setup
    login(client)
    db.set_meta("team", "RKM")
    clip_id, _ = add_clip(cfg, db)
    db.mark_done("1", 1)
    clip = client.get("http://localhost/api/state").get_json()["vods"][0]["clips"][0]
    assert clip["tiktok"]["status"] == "" and "RKM" in clip["tiktok"]["caption"]
    assert "Gol 2 de RKM" in clip["tiktok"]["caption"] and "#" in clip["tiktok"]["caption"]


def test_creating_the_tiktok_version_queues_it_and_downloads_when_ready(setup):
    cfg, _, client, worker, db = setup
    login(client)
    clip_id, path = add_clip(cfg, db)
    assert client.get(f"http://localhost/media/tiktok/{clip_id}").status_code == 404   # aún no existe

    assert client.post(f"http://localhost/api/clips/{clip_id}/tiktok").status_code == 200
    assert worker.renders == [clip_id]
    state = client.get("http://localhost/api/state").get_json()
    assert state["vods"][0]["clips"][0]["tiktok"]["status"] == "queued"

    vertical = path.with_name(path.stem + "_tiktok.mp4")
    vertical.write_bytes(b"vertical")
    db.set_tiktok(clip_id, "ready", path=vertical)
    watch = client.get(f"http://localhost/media/tiktok/{clip_id}")
    assert watch.data == b"vertical"
    dl = client.get(f"http://localhost/media/tiktok/{clip_id}?download=1")
    assert "attachment" in dl.headers["Content-Disposition"] and "_tiktok.mp4" in dl.headers["Content-Disposition"]
    watch.close()   # en Windows un archivo servido no se puede borrar hasta cerrar la respuesta
    dl.close()

    vertical.unlink()   # si se borra a mano, el panel deja de darla por lista
    assert client.get("http://localhost/api/state").get_json()["vods"][0]["clips"][0]["tiktok"]["status"] == ""
    assert client.get(f"http://localhost/media/tiktok/{clip_id}").status_code == 404


def test_tiktok_refuses_when_the_original_clip_is_gone(setup):
    cfg, _, client, worker, db = setup
    login(client)
    clip_id, _ = add_clip(cfg, db, exists=False)
    resp = client.post(f"http://localhost/api/clips/{clip_id}/tiktok")
    assert resp.status_code == 409 and worker.renders == []


def test_tiktok_for_the_whole_vod_skips_the_ones_already_done(setup):
    cfg, _, client, worker, db = setup
    login(client)
    a, _ = add_clip(cfg, db, new=1)
    b, _ = add_clip(cfg, db, new=2)
    c, _ = add_clip(cfg, db, new=3)
    db.set_tiktok(a, "ready", path=cfg.clips_dir / "x.mp4")
    assert client.post("http://localhost/api/vods/1/tiktok").get_json() == {"queued": 2}
    assert sorted(worker.renders) == sorted([b, c])


# --- envío a TikTok como borrador --------------------------------------------


def connect_tiktok(db):
    now = time.time()
    db.save_tiktok_token({"access": "A", "refresh": "R", "expires_at": now + 3600,
                          "refresh_expires_at": now + 86400, "open_id": "o"})
    db.set_meta("tiktok_user", {"open_id": "o", "display_name": "Kelvin", "avatar_url": ""})


def add_ready_clip(cfg, db, new=1):
    clip_id, path = add_clip(cfg, db, new=new)
    vertical = path.with_name(path.stem + "_tiktok.mp4")
    vertical.write_bytes(b"vertical")
    db.set_tiktok(clip_id, "ready", path=vertical)
    return clip_id


def test_state_reports_tiktok_connection(setup):
    _, _, client, _, db = setup
    login(client)
    assert client.get("http://localhost/api/state").get_json()["tiktok"] == {
        "configured": True, "connected": False, "user": "", "left": 5, "limit": 5}
    connect_tiktok(db)
    state = client.get("http://localhost/api/state").get_json()["tiktok"]
    assert state["connected"] and state["user"] == "Kelvin"


def test_tiktok_login_start_redirects_to_tiktok_with_pkce(setup):
    _, _, client, _, _ = setup
    login(client)
    resp = client.get("http://localhost/auth/tiktok/start")
    assert resp.status_code == 302 and resp.location.startswith("https://www.tiktok.com/v2/auth/authorize/")
    assert "code_challenge_method=S256" in resp.location and "client_key=ck" in resp.location
    with client.session_transaction() as s:
        assert s["tiktok"]["state"] in resp.location


def test_tiktok_login_without_keys_explains_what_is_missing(setup, monkeypatch):
    cfg, app, client, _, _ = setup
    login(client)
    monkeypatch.setattr(type(cfg), "tiktok_configured", property(lambda self: False))
    resp = client.get("http://localhost/auth/tiktok/start")
    assert resp.status_code == 302 and "tiktok_error" in resp.location and "TIKTOK_CLIENT_KEY" in unquote(resp.location)


def test_tiktok_callback_rejects_a_foreign_state(setup):
    _, _, client, _, db = setup
    login(client)
    client.get("http://localhost/auth/tiktok/start")
    resp = client.get("http://localhost/auth/tiktok/callback?code=c&state=otro")
    assert "tiktok_error" in resp.location and db.get_tiktok_token() is None


def test_tiktok_callback_connects_the_account(setup, monkeypatch):
    from rkm_clips import tiktok
    from rkm_clips.tiktok_api import TikTokUser

    class FakeClient:
        def exchange_code(self, code, verifier):
            assert code == "c" and verifier
            connect_tiktok(Database(setup_cfg.db_path))
            return TikTokUser("o", "Kelvin")

    cfg, _, client, _, db = setup
    setup_cfg = cfg
    login(client)
    monkeypatch.setattr(tiktok, "client_for", lambda cfg, db: FakeClient())
    client.get("http://localhost/auth/tiktok/start")
    with client.session_transaction() as s:
        state = s["tiktok"]["state"]
    resp = client.get(f"http://localhost/auth/tiktok/callback?code=c&state={state}")
    assert resp.status_code == 302 and "tiktok=ok" in resp.location
    assert client.get("http://localhost/api/state").get_json()["tiktok"]["user"] == "Kelvin"


def test_sending_needs_a_connected_account_and_a_vertical_version(setup):
    cfg, _, client, worker, db = setup
    login(client)
    clip_id = add_ready_clip(cfg, db)
    assert client.post(f"http://localhost/api/clips/{clip_id}/tiktok/send").status_code == 409  # sin conectar
    connect_tiktok(db)
    raw_id, _ = add_clip(cfg, db, new=2)   # sin versión vertical
    assert client.post(f"http://localhost/api/clips/{raw_id}/tiktok/send").status_code == 409
    assert client.post(f"http://localhost/api/clips/{clip_id}/tiktok/send").status_code == 200
    assert worker.sends == [clip_id]
    assert client.post(f"http://localhost/api/clips/{clip_id}/tiktok/send").status_code == 409  # ya en cola


def test_sending_stops_at_the_daily_draft_limit(setup):
    cfg, _, client, worker, db = setup
    login(client)
    connect_tiktok(db)
    sent = [add_ready_clip(cfg, db, new=n) for n in range(1, 6)]
    for clip_id in sent:
        db.set_draft(clip_id, "sent")
    extra = add_ready_clip(cfg, db, new=6)
    resp = client.post(f"http://localhost/api/clips/{extra}/tiktok/send")
    assert resp.status_code == 409 and "5 borradores" in resp.get_json()["error"]
    assert client.post("http://localhost/api/vods/1/tiktok/send").status_code == 409


def test_sending_a_whole_vod_sends_only_what_fits_today(setup):
    cfg, _, client, worker, db = setup
    login(client)
    connect_tiktok(db)
    ids = [add_ready_clip(cfg, db, new=n) for n in range(1, 8)]
    for clip_id in ids[:2]:
        db.set_draft(clip_id, "sent")   # ya hay 2 enviados hoy: caben 3 más
    resp = client.post("http://localhost/api/vods/1/tiktok/send").get_json()
    assert resp == {"queued": 3, "remaining": 2}
    assert worker.sends == ids[2:5]


def test_send_draft_marks_the_clip_sent_or_failed(setup, tmp_path):
    from rkm_clips import tiktok
    from rkm_clips.tiktok_api import TikTokError

    class FakeClient:
        def __init__(self, fail=None):
            self.fail, self.uploaded = fail, []

        def upload_draft(self, path):
            if self.fail:
                raise self.fail
            self.uploaded.append(path.name)
            return "pub1"

        def wait_for_inbox(self, publish_id):
            assert publish_id == "pub1"

    cfg, _, _, _, db = setup
    ok, bad = add_ready_clip(cfg, db, new=1), add_ready_clip(cfg, db, new=2)
    client = FakeClient()
    assert tiktok.send_draft(cfg, db, ok, client) is True
    row = db.get_clip(ok)
    assert row["draft_status"] == "sent" and row["draft_publish_id"] == "pub1" and row["draft_sent_at"]
    assert client.uploaded == ["clip1_tiktok.mp4"]

    assert tiktok.send_draft(cfg, db, bad, FakeClient(fail=TikTokError("TikTok dijo que no"))) is False
    row = db.get_clip(bad)
    assert row["draft_status"] == "error" and "dijo que no" in row["draft_error"]
    assert db.drafts_sent_since(0) == 1   # un fallo no cuenta para el límite


def test_a_restart_clears_drafts_left_mid_upload(setup):
    cfg, _, _, _, db = setup
    clip_id = add_ready_clip(cfg, db)
    db.set_draft(clip_id, "uploading")
    assert db.reset_draft_interrupted() == 1 and db.get_clip(clip_id)["draft_status"] == ""
