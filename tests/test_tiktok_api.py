import hashlib
import time
from urllib.parse import parse_qs, urlparse

import pytest

from rkm_clips import tiktok_api
from rkm_clips.db import Database
from rkm_clips.tiktok_api import NotConnected, TikTokClient, TikTokError


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body if body is not None else {}, text

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

    def post(self, url, **kw):
        return self._answer("POST", url, kw)

    def put(self, url, **kw):
        return self._answer("PUT", url, kw)

    def request(self, method, url, **kw):
        return self._answer(method, url, kw)


TOKEN = {"access_token": "A", "refresh_token": "R", "expires_in": 86400, "refresh_expires_in": 31536000,
         "open_id": "oid", "scope": "user.info.basic,video.upload", "token_type": "Bearer"}
USER = {"data": {"user": {"open_id": "oid", "display_name": "Kelvin", "avatar_url": "u"}}, "error": {"code": "ok"}}


def make(tmp_path, responses):
    db = Database(tmp_path / "a.sqlite3")
    client = TikTokClient("ck", "cs", "http://localhost:8765/auth/tiktok/callback", token_store=db)
    client.session = FakeSession(responses)
    return client, db


def connect(db, expires_in=3600):
    now = time.time()
    db.save_tiktok_token({"access": "A", "refresh": "R", "expires_at": now + expires_in,
                          "refresh_expires_at": now + 86400, "open_id": "oid"})


def test_challenge_is_hex_sha256_not_base64():
    verifier = tiktok_api.new_verifier()
    assert 43 <= len(verifier) <= 128
    assert tiktok_api.challenge_for(verifier) == hashlib.sha256(verifier.encode()).hexdigest()


def test_authorize_url_has_pkce_scopes_and_exact_redirect(tmp_path):
    client, _ = make(tmp_path, {})
    q = parse_qs(urlparse(client.authorize_url("st", "v" * 50)).query)
    assert q["client_key"] == ["ck"] and q["state"] == ["st"] and q["response_type"] == ["code"]
    assert q["scope"] == ["user.info.basic,video.upload"]
    assert q["redirect_uri"] == ["http://localhost:8765/auth/tiktok/callback"]
    assert q["code_challenge_method"] == ["S256"] and len(q["code_challenge"][0]) == 64


def test_exchange_code_sends_verifier_and_stores_tokens(tmp_path):
    client, db = make(tmp_path, {("POST", "/oauth/token"): [Resp(200, TOKEN)], ("GET", "/user/info"): [Resp(200, USER)]})
    user = client.exchange_code("code", "verifier")
    sent = client.session.calls[0][2]["data"]
    assert sent["code_verifier"] == "verifier" and sent["client_secret"] == "cs" and sent["grant_type"] == "authorization_code"
    assert user.display_name == "Kelvin"
    token = db.get_tiktok_token()
    assert token["access"] == "A" and token["refresh"] == "R" and token["expires_at"] > time.time() + 80000


def test_rejected_login_raises(tmp_path):
    client, _ = make(tmp_path, {("POST", "/oauth/token"): [Resp(400, {"error": "invalid_grant", "error_description": "malo"})]})
    with pytest.raises(TikTokError, match="malo"):
        client.exchange_code("c", "v")


def test_expired_token_is_refreshed_and_rotated(tmp_path):
    client, db = make(tmp_path, {("POST", "/oauth/token"): [Resp(200, {**TOKEN, "access_token": "B", "refresh_token": "R2"})],
                                 ("GET", "/user/info"): [Resp(200, USER)]})
    connect(db, expires_in=-10)
    client.get_user()
    assert client.session.calls[0][2]["data"]["refresh_token"] == "R"
    assert db.get_tiktok_token()["refresh"] == "R2"
    assert client.session.calls[1][2]["headers"]["Authorization"] == "Bearer B"


def test_dead_refresh_token_disconnects(tmp_path):
    client, db = make(tmp_path, {("POST", "/oauth/token"): [Resp(400, {"error": "invalid_grant"})]})
    connect(db, expires_in=-10)
    with pytest.raises(NotConnected):
        client.get_user()
    assert db.get_tiktok_token() is None


def test_not_connected_without_token(tmp_path):
    client, _ = make(tmp_path, {})
    with pytest.raises(NotConnected):
        client.get_user()


def test_chunk_plan():
    assert tiktok_api.chunk_plan(14_000_000) == (14_000_000, 1)
    size = 100 * 1024 * 1024
    chunk, count = tiktok_api.chunk_plan(size)
    assert chunk == 32 * 1024 * 1024 and count == 3  # el último absorbe el resto (36 MB)


def init_ok(upload_url="https://upload.example/x?t=1"):
    return Resp(200, {"data": {"publish_id": "v_pub_1", "upload_url": upload_url}, "error": {"code": "ok"}})


def test_upload_draft_single_chunk(tmp_path):
    client, db = make(tmp_path, {("POST", "/inbox/video/init"): [init_ok()], ("PUT", "upload.example"): [Resp(201)]})
    connect(db)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 1000)
    assert client.upload_draft(video) == "v_pub_1"
    init = client.session.calls[0][2]["json"]["source_info"]
    assert init == {"source": "FILE_UPLOAD", "video_size": 1000, "chunk_size": 1000, "total_chunk_count": 1}
    put = client.session.calls[1][2]["headers"]
    assert put["Content-Range"] == "bytes 0-999/1000" and put["Content-Length"] == "1000" and put["Content-Type"] == "video/mp4"


def test_upload_draft_multiple_chunks_cover_the_whole_file(tmp_path, monkeypatch):
    monkeypatch.setattr(tiktok_api, "MAX_SINGLE", 100)
    monkeypatch.setattr(tiktok_api, "CHUNK", 40)
    client, db = make(tmp_path, {("POST", "/inbox/video/init"): [init_ok()], ("PUT", "upload.example"): [Resp(206), Resp(206), Resp(201)]})
    connect(db)
    video = tmp_path / "v.mp4"
    video.write_bytes(bytes(range(130)))  # 130 // 40 = 3 bloques: 40 + 40 + 50
    client.upload_draft(video)
    ranges = [c[2]["headers"]["Content-Range"] for c in client.session.calls if c[0] == "PUT"]
    assert ranges == ["bytes 0-39/130", "bytes 40-79/130", "bytes 80-129/130"]
    sent = b"".join(c[2]["data"] for c in client.session.calls if c[0] == "PUT")
    assert sent == bytes(range(130))


def test_pending_limit_has_friendly_message(tmp_path):
    body = {"data": {}, "error": {"code": "spam_risk_too_many_pending_share", "message": "x"}}
    client, db = make(tmp_path, {("POST", "/inbox/video/init"): [Resp(403, body)]})
    connect(db)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(TikTokError, match="5 borradores"):
        client.upload_draft(video)


def test_invalid_access_token_asks_to_reconnect(tmp_path):
    client, db = make(tmp_path, {("POST", "/inbox/video/init"): [Resp(401, {"error": {"code": "access_token_invalid"}})]})
    connect(db)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(NotConnected):
        client.upload_draft(video)


def status(name, reason=""):
    return Resp(200, {"data": {"status": name, "fail_reason": reason}, "error": {"code": "ok"}})


def test_wait_for_inbox_polls_until_sent(tmp_path):
    client, db = make(tmp_path, {("POST", "/status/fetch"): [status("PROCESSING_UPLOAD"), status("SEND_TO_USER_INBOX")]})
    connect(db)
    client.wait_for_inbox("p", interval_s=0)
    assert len(client.session.calls) == 2


def test_wait_for_inbox_reports_failure(tmp_path):
    client, db = make(tmp_path, {("POST", "/status/fetch"): [status("FAILED", "file_format_check_failed")]})
    connect(db)
    with pytest.raises(TikTokError, match="file_format_check_failed"):
        client.wait_for_inbox("p", interval_s=0)
