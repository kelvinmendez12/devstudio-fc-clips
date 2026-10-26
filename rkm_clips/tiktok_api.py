"""Cliente mínimo de la API oficial de TikTok: login (Login Kit de escritorio, con PKCE) y envío
de vídeos como *borrador* a la bandeja del usuario (Content Posting API, scope video.upload).

La app nunca publica: el borrador aparece en TikTok y el usuario decide si lo publica.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
API_URL = "https://open.tiktokapis.com"
SCOPES = "user.info.basic,video.upload"

# Bloques de subida: TikTok pide entre 5 y 64 MB (el último puede llegar a 128 MB); un vídeo
# más pequeño que un bloque va entero.
MAX_SINGLE = 64 * 1024 * 1024
CHUNK = 32 * 1024 * 1024

# Máximo de borradores pendientes por usuario y día, según TikTok.
DRAFT_LIMIT = 5
DRAFT_WINDOW_S = 24 * 3600

# Igual que en Twitch: el token de renovación puede cambiar al usarlo, así que se serializa.
_REFRESH_LOCK = threading.Lock()


class TikTokError(Exception):
    pass


class NotConnected(TikTokError):
    """No hay sesión de TikTok guardada, o caducó y hay que volver a conectar."""


@dataclass(frozen=True)
class TikTokUser:
    open_id: str
    display_name: str
    avatar_url: str = ""


FRIENDLY = {
    "spam_risk_too_many_pending_share": "TikTok ya tiene 5 borradores pendientes de esta cuenta en las últimas "
                                        "24 h: publica o descarta alguno en la app de TikTok y vuelve a intentarlo",
    "spam_risk_user_banned_from_posting": "TikTok no deja a esta cuenta recibir vídeos ahora mismo",
    "rate_limit_exceeded": "Demasiadas peticiones a TikTok; espera un minuto",
    "scope_not_authorized": "Falta el permiso de subir vídeos: reconecta TikTok",
}


# --- PKCE ---------------------------------------------------------------

def new_verifier() -> str:
    return secrets.token_urlsafe(64)[:96]  # 43–128 caracteres sin reservados


def challenge_for(verifier: str) -> str:
    # TikTok pide el SHA-256 en hexadecimal (no en base64url como el RFC 7636).
    return hashlib.sha256(verifier.encode("ascii")).hexdigest()


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) para un vídeo de `size` bytes."""
    if size <= MAX_SINGLE:
        return size, 1
    return CHUNK, size // CHUNK  # el último bloque absorbe el resto


class TikTokClient:
    def __init__(self, client_key: str, client_secret: str, redirect_uri: str, token_store=None,
                 timeout: float = 30):
        self.client_key = client_key
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        # Objeto con get_tiktok_token() / save_tiktok_token(dict) / clear_tiktok().
        self.token_store = token_store
        self.timeout = timeout
        self.session = requests.Session()

    # --- login ---------------------------------------------------------

    def authorize_url(self, state: str, verifier: str) -> str:
        return AUTHORIZE_URL + "?" + urlencode({
            "client_key": self.client_key, "response_type": "code", "scope": SCOPES,
            "redirect_uri": self.redirect_uri, "state": state,
            "code_challenge": challenge_for(verifier), "code_challenge_method": "S256",
        })

    def _token_request(self, data: dict) -> dict:
        resp = self.session.post(
            f"{API_URL}/v2/oauth/token/",
            data={"client_key": self.client_key, "client_secret": self.client_secret, **data},
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
            timeout=self.timeout,
        )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or "access_token" not in body:
            detail = body.get("error_description") or body.get("error") or resp.text[:150]
            raise TikTokError(f"TikTok rechazó el login ({resp.status_code}): {detail}")
        return body

    def _save(self, body: dict) -> None:
        now = time.time()
        self.token_store.save_tiktok_token({
            "access": body["access_token"],
            "refresh": body["refresh_token"],
            # Margen de 5 min para no usar un token a punto de caducar.
            "expires_at": now + int(body.get("expires_in", 0)) - 300,
            "refresh_expires_at": now + int(body.get("refresh_expires_in", 0)),
            "open_id": body.get("open_id", ""),
        })

    def exchange_code(self, code: str, verifier: str) -> TikTokUser:
        body = self._token_request({
            "code": code, "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri, "code_verifier": verifier,
        })
        self._save(body)
        return self.get_user()

    def _access_token(self) -> str:
        if self.token_store is None:
            raise NotConnected("Conecta tu cuenta de TikTok")
        token = self.token_store.get_tiktok_token()
        if not token:
            raise NotConnected("Conecta tu cuenta de TikTok")
        if token["expires_at"] > time.time():
            return token["access"]
        with _REFRESH_LOCK:
            current = self.token_store.get_tiktok_token()
            if not current:
                raise NotConnected("Conecta tu cuenta de TikTok")
            if current["expires_at"] > time.time():
                return current["access"]  # otro hilo ya lo renovó
            if current["refresh_expires_at"] <= time.time():
                self.token_store.clear_tiktok()
                raise NotConnected("La sesión de TikTok caducó: vuelve a conectar tu cuenta")
            try:
                body = self._token_request({"grant_type": "refresh_token", "refresh_token": current["refresh"]})
            except TikTokError as exc:
                if "(400)" in str(exc) or "(401)" in str(exc):
                    self.token_store.clear_tiktok()
                    raise NotConnected("La sesión de TikTok caducó: vuelve a conectar tu cuenta") from exc
                raise
            self._save(body)
            return body["access_token"]

    # --- peticiones a la API -----------------------------------------------

    def _api(self, method: str, path: str, **kwargs) -> dict:
        resp = self.session.request(
            method, f"{API_URL}{path}",
            headers={"Authorization": f"Bearer {self._access_token()}",
                     "Content-Type": "application/json; charset=UTF-8"},
            timeout=self.timeout, **kwargs,
        )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        error = body.get("error") or {}
        code = error.get("code", "ok" if resp.status_code == 200 else "")
        if resp.status_code == 401 or code in ("access_token_invalid", "token_expired"):
            raise NotConnected("TikTok no acepta la sesión: vuelve a conectar tu cuenta")
        if resp.status_code != 200 or code != "ok":
            message = FRIENDLY.get(code) or error.get("message") or resp.text[:150]
            raise TikTokError(f"{message} ({code or resp.status_code})")
        return body.get("data") or {}

    def get_user(self) -> TikTokUser:
        data = self._api("GET", "/v2/user/info/?fields=open_id,display_name,avatar_url").get("user") or {}
        token = self.token_store.get_tiktok_token() or {}
        return TikTokUser(data.get("open_id") or token.get("open_id", ""),
                          data.get("display_name") or "TikTok", data.get("avatar_url", ""))

    # --- borradores ------------------------------------------------------

    def upload_draft(self, path: Path) -> str:
        """Sube un vídeo a la bandeja de borradores del usuario y devuelve el publish_id."""
        size = path.stat().st_size
        chunk_size, count = chunk_plan(size)
        data = self._api("POST", "/v2/post/publish/inbox/video/init/", json={
            "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                            "chunk_size": chunk_size, "total_chunk_count": count},
        })
        publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
        if not publish_id or not upload_url:
            raise TikTokError("TikTok no devolvió dónde subir el vídeo")
        with path.open("rb") as f:
            for i in range(count):
                start = i * chunk_size
                end = size if i == count - 1 else start + chunk_size  # el último incluye el resto
                f.seek(start)
                part = f.read(end - start)
                resp = self.session.put(
                    upload_url, data=part, timeout=300,
                    headers={"Content-Type": "video/mp4", "Content-Length": str(len(part)),
                             "Content-Range": f"bytes {start}-{end - 1}/{size}"},
                )
                if resp.status_code not in (200, 201, 206):
                    raise TikTokError(f"La subida a TikTok falló ({resp.status_code}): {resp.text[:150]}")
        return publish_id

    def draft_status(self, publish_id: str) -> tuple[str, str]:
        """(estado, motivo del fallo). Estados: PROCESSING_UPLOAD, SEND_TO_USER_INBOX, FAILED…"""
        data = self._api("POST", "/v2/post/publish/status/fetch/", json={"publish_id": publish_id})
        return data.get("status", ""), data.get("fail_reason", "")

    def wait_for_inbox(self, publish_id: str, timeout_s: float = 90, interval_s: float = 3) -> None:
        """Espera a que el borrador esté en la bandeja de TikTok del usuario."""
        deadline = time.time() + timeout_s
        status = ""
        while time.time() < deadline:
            status, reason = self.draft_status(publish_id)
            if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
                return
            if status == "FAILED":
                raise TikTokError(f"TikTok no pudo procesar el vídeo: {reason or 'sin motivo'}")
            time.sleep(interval_s)
        raise TikTokError(f"TikTok tarda en procesar el vídeo (estado: {status or 'desconocido'}); "
                          "mira la bandeja de TikTok en unos minutos")
