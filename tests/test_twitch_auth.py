import time

import pytest

from rkm_clips.db import Database
from rkm_clips.twitch import NotLoggedIn, TwitchClient, TwitchError


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text, self.headers = status, body or {}, text, {}

    def json(self):
        return self._body


class FakeSession:
    """Respuestas encoladas por (método, parte de la URL); guarda las peticiones hechas."""

    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def _answer(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        for (m, part), queue in self.responses.items():
            if m == method and part in url and queue:
                return queue.pop(0)
        raise AssertionError(f"petición inesperada: {method} {url}")

    def get(self, url, **kw):
        return self._answer("GET", url, kw)

    def post(self, url, **kw):
        return self._answer("POST", url, kw)


def make(tmp_path, responses):
    db = Database(tmp_path / "a.sqlite3")
    client = TwitchClient("cid", token_store=db)
    client.session = FakeSession(responses)
    return client, db


def test_device_login_start_sends_only_client_id(tmp_path):
    client, _ = make(tmp_path, {("POST", "/device"): [Resp(200, {
        "device_code": "d", "user_code": "U1", "verification_uri": "https://www.twitch.tv/activate?device-code=U1",
        "expires_in": 1800, "interval": 5})]})
    code = client.start_device_login()
    assert (code.user_code, code.interval) == ("U1", 5)
    sent = client.session.calls[0][2]["data"]
    assert sent == {"client_id": "cid", "scopes": ""}  # app pública: sin secreto ni permisos


def test_device_poll_pending_then_authorized_saves_tokens(tmp_path):
    me = {"data": [{"id": "9", "login": "club", "display_name": "Club", "profile_image_url": "x"}]}
    client, db = make(tmp_path, {
        ("POST", "/token"): [Resp(400, {"message": "authorization_pending"}),
                             Resp(200, {"access_token": "A", "refresh_token": "R", "expires_in": 14000})],
        ("GET", "/users"): [Resp(200, me)],
    })
    assert client.poll_device_login("d") is None
    user = client.poll_device_login("d")
    assert (user.id, user.login) == ("9", "club")
    saved = db.get_user_token()
    assert (saved["access"], saved["refresh"]) == ("A", "R")
    assert saved["expires_at"] > time.time() + 13000


def test_device_poll_error_raises(tmp_path):
    client, _ = make(tmp_path, {("POST", "/token"): [Resp(400, {"message": "invalid device code"})]})
    with pytest.raises(TwitchError, match="invalid device code"):
        client.poll_device_login("d")


def test_expired_token_is_refreshed_and_new_refresh_token_is_saved(tmp_path):
    client, db = make(tmp_path, {
        ("POST", "/token"): [Resp(200, {"access_token": "A2", "refresh_token": "R2", "expires_in": 14000})],
        ("GET", "/videos"): [Resp(200, {"data": []})],
    })
    db.save_user_token("A1", "R1", expires_at=time.time() - 10)
    assert client.get_archives("1") == []
    refresh = client.session.calls[0][2]["data"]
    assert refresh == {"client_id": "cid", "grant_type": "refresh_token", "refresh_token": "R1"}
    assert client.session.calls[1][2]["headers"]["Authorization"] == "Bearer A2"
    assert db.get_user_token()["refresh"] == "R2"  # el de renovación es de un solo uso


def test_valid_token_is_not_refreshed(tmp_path):
    client, db = make(tmp_path, {("GET", "/streams"): [Resp(200, {"data": []})]})
    db.save_user_token("A1", "R1", expires_at=time.time() + 3600)
    assert client.get_live_stream_id("1") is None
    assert [c[0] for c in client.session.calls] == ["GET"]


def test_revoked_refresh_token_asks_to_reconnect(tmp_path):
    client, db = make(tmp_path, {("POST", "/token"): [Resp(400, {"message": "Invalid refresh token"})]})
    db.save_user_token("A1", "R1", expires_at=time.time() - 10)
    db.set_meta("user", {"id": "1"})
    with pytest.raises(NotLoggedIn):
        client.get_archives("1")
    assert db.get_user_token() is None and db.get_meta("user") is None


def test_no_session_raises_not_logged_in(tmp_path):
    client, _ = make(tmp_path, {})
    with pytest.raises(NotLoggedIn):
        client.get_archives("1")


def test_401_from_helix_triggers_one_refresh_and_retry(tmp_path):
    client, db = make(tmp_path, {
        ("GET", "/videos"): [Resp(401), Resp(200, {"data": []})],
        ("POST", "/token"): [Resp(200, {"access_token": "A2", "refresh_token": "R2", "expires_in": 14000})],
    })
    db.save_user_token("A1", "R1", expires_at=time.time() + 3600)
    assert client.get_archives("1") == []
    assert db.get_user_token()["access"] == "A2"
