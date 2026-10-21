"""Cliente mínimo de la Helix API de Twitch con login por código de dispositivo.

La app es un cliente *público* de Twitch: no lleva secreto. El usuario introduce un código en
twitch.tv/activate (Device Code Grant Flow) y la app guarda su token de usuario y su token de
renovación. No se pide ningún permiso (scope): solo se leen datos públicos de su canal.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX_URL = "https://api.twitch.tv/helix"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# Los tokens de renovación de Twitch son de un solo uso: si el poller y el panel renuevan a la
# vez, el segundo se quedaría con un token ya gastado. Se serializa y se relee antes de renovar.
_REFRESH_LOCK = threading.Lock()


class TwitchError(Exception):
    pass


class NotLoggedIn(TwitchError):
    """No hay sesión de Twitch guardada, o caducó y hay que volver a conectar."""


@dataclass(frozen=True)
class Vod:
    id: str
    stream_id: str | None
    title: str
    created_at: str  # ISO 8601, UTC
    url: str
    duration_s: int
    thumbnail_url: str = ""

    def thumbnail(self, width: int = 640, height: int = 360) -> str:
        return thumbnail(self.thumbnail_url, width, height)


@dataclass(frozen=True)
class TwitchUser:
    id: str
    login: str
    display_name: str
    profile_image_url: str


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


def thumbnail(template: str, width: int = 640, height: int = 360) -> str:
    # Helix devuelve la miniatura con marcadores %{width}x%{height}.
    return (template or "").replace("%{width}", str(width)).replace("%{height}", str(height))


_DURATION_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def parse_duration(text: str) -> int:
    """Convierte la duración de Helix ("3h2m10s", "45m3s", "12s") a segundos."""
    match = _DURATION_RE.fullmatch(text or "")
    if not match or not any(match.groups()):
        return 0
    h, m, s = (int(g) if g else 0 for g in match.groups())
    return h * 3600 + m * 60 + s


class TwitchClient:
    def __init__(self, client_id: str, token_store=None, timeout: float = 20):
        self.client_id = client_id
        # Objeto con get_user_token() / save_user_token(access, refresh, expires_at).
        self.token_store = token_store
        self.timeout = timeout
        self.session = requests.Session()

    # --- login por código de dispositivo --------------------------------

    def start_device_login(self) -> DeviceCode:
        resp = self.session.post(DEVICE_URL, data={"client_id": self.client_id, "scopes": ""},
                                 timeout=self.timeout)
        if resp.status_code != 200:
            raise TwitchError(f"Twitch no pudo iniciar el login ({resp.status_code}): {resp.text[:200]}")
        b = resp.json()
        return DeviceCode(b["device_code"], b["user_code"], b["verification_uri"],
                          int(b.get("expires_in", 1800)), int(b.get("interval", 5)))

    def poll_device_login(self, device_code: str) -> TwitchUser | None:
        """None mientras el usuario no haya autorizado; el usuario al autorizar.

        Lanza TwitchError si el código caducó o el usuario lo rechazó.
        """
        resp = self.session.post(
            TOKEN_URL,
            data={"client_id": self.client_id, "scopes": "", "device_code": device_code,
                  "grant_type": DEVICE_GRANT},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            message = ""
            try:
                message = resp.json().get("message", "")
            except ValueError:
                pass
            if resp.status_code == 400 and message in ("authorization_pending", "slow_down"):
                return None
            raise TwitchError(f"Twitch rechazó el login ({resp.status_code}): {message or resp.text[:150]}")
        body = resp.json()
        self._save(body)
        return self.get_me()

    # --- tokens ----------------------------------------------------------

    def _save(self, body: dict) -> None:
        # Margen de 5 min para no usar un token a punto de caducar.
        self.token_store.save_user_token(
            body["access_token"], body["refresh_token"], time.time() + int(body.get("expires_in", 0)) - 300
        )

    def _access_token(self, force_refresh: bool = False) -> str:
        if self.token_store is None:
            raise NotLoggedIn("Conecta tu cuenta de Twitch")
        token = self.token_store.get_user_token()
        if token and not force_refresh and token["expires_at"] > time.time():
            return token["access"]
        with _REFRESH_LOCK:
            current = self.token_store.get_user_token()  # otro hilo pudo renovarlo mientras esperábamos
            if not current:
                raise NotLoggedIn("Conecta tu cuenta de Twitch")
            if not force_refresh and current["expires_at"] > time.time():
                return current["access"]
            if force_refresh and token and current["access"] != token["access"] and current["expires_at"] > time.time():
                return current["access"]
            resp = self.session.post(
                TOKEN_URL,
                data={"client_id": self.client_id, "grant_type": "refresh_token",
                      "refresh_token": current["refresh"]},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                if resp.status_code in (400, 401):
                    # Más de 30 días sin usar la app, o acceso revocado desde Twitch.
                    self.token_store.clear_user_token()
                    raise NotLoggedIn("La sesión de Twitch caducó: vuelve a conectar tu cuenta")
                raise TwitchError(f"No se pudo renovar la sesión de Twitch ({resp.status_code})")
            body = resp.json()
            self._save(body)
            return body["access_token"]

    def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in (1, 2):
            resp = self.session.get(
                f"{HELIX_URL}/{path}",
                params=params or {},
                headers={"Client-Id": self.client_id,
                         "Authorization": f"Bearer {self._access_token(force_refresh=attempt == 2)}"},
                timeout=self.timeout,
            )
            if resp.status_code == 401 and attempt == 1:
                log.info("Token rechazado por Twitch, renovándolo")
                continue
            if resp.status_code == 429:
                reset = float(resp.headers.get("Ratelimit-Reset", time.time() + 5))
                raise TwitchError(f"Límite de peticiones de Twitch; reintenta tras {reset - time.time():.0f} s")
            if resp.status_code != 200:
                raise TwitchError(f"GET {path} → {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        raise NotLoggedIn("La sesión de Twitch no es válida: vuelve a conectar tu cuenta")

    # --- endpoints -----------------------------------------------------

    def get_me(self) -> TwitchUser:
        data = self._get("users")["data"]  # sin parámetros: el usuario del token
        if not data:
            raise TwitchError("No se pudo leer tu usuario de Twitch")
        u = data[0]
        return TwitchUser(u["id"], u["login"], u.get("display_name") or u["login"],
                          u.get("profile_image_url", ""))

    def get_user_id(self, login: str) -> str:
        data = self._get("users", {"login": login})["data"]
        if not data:
            raise TwitchError(f"El canal {login!r} no existe en Twitch")
        return data[0]["id"]

    def get_live_stream_id(self, user_id: str) -> str | None:
        data = self._get("streams", {"user_id": user_id, "type": "live"})["data"]
        return data[0]["id"] if data else None

    def get_archives(self, user_id: str, limit: int = 20) -> list[Vod]:
        """VODs de emisiones pasadas (type=archive), del más reciente al más antiguo."""
        data = self._get("videos", {"user_id": user_id, "type": "archive", "first": min(limit, 100)})["data"]
        return [_to_vod(v) for v in data]

    def get_video(self, video_id: str) -> Vod:
        data = self._get("videos", {"id": video_id})["data"]
        if not data:
            raise TwitchError(f"El VOD {video_id} no existe o ya no está disponible")
        return _to_vod(data[0])


def _to_vod(v: dict) -> Vod:
    return Vod(
        id=v["id"],
        stream_id=v.get("stream_id"),
        title=v.get("title", ""),
        created_at=v["created_at"],
        url=v["url"],
        duration_s=parse_duration(v.get("duration", "")),
        thumbnail_url=v.get("thumbnail_url", ""),
    )
