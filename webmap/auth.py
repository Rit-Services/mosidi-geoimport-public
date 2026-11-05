"""Anmeldung für die Demo-Karte, wenn sie über das Internet erreichbar ist (Demonstrator).

- Login-Formular unter /login setzt ein signiertes Sitzungs-Cookie (HMAC-SHA256, HttpOnly, SameSite=Strict,
  Secure; nur für eine lokale Vorführung über http abschaltbar). Keine Datenbank, keine Sitzungstabelle.
- CSRF-Schutz: Login-Formular mit Double-Submit-Token (Zufallswert im Cookie, HMAC davon im Formular),
  Abmelden nur per POST mit einem an die Sitzung gebundenen Token.
- Sperre je Client-Adresse nach zu vielen Fehlversuchen und zusätzlich eine globale Obergrenze je Minute,
  damit wechselnde Adressen (X-Forwarded-For) keine unbegrenzten Versuche ergeben.
- Ohne MAP_AUTH_PASSWORD ist die Anmeldung aus (Compose-Setup, nur 127.0.0.1). Mit MAP_AUTH_REQUIRED=true
  startet die Karte ohne gültige Zugangsdaten nicht (Kubernetes).

Umgebungsvariablen: MAP_AUTH_USER, MAP_AUTH_PASSWORD, MAP_SESSION_SECRET, MAP_AUTH_REQUIRED,
MAP_SESSION_HOURS, MAP_LOGIN_MAX_ATTEMPTS, MAP_LOGIN_LOCK_MINUTES, MAP_LOGIN_GLOBAL_MAX, MAP_COOKIE_SECURE.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field

log = logging.getLogger("webmap.auth")

COOKIE_NAME = "geoimport_session"
CSRF_COOKIE = "geoimport_csrf"
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class AuthConfig:
    """Zugangsdaten und Sitzungs-Einstellungen der Demo-Karte."""

    user: str
    password: str
    secret: bytes
    session_hours: float = 12.0
    max_attempts: int = 5
    lock_minutes: float = 15.0
    global_max: int = 30  # Fehlversuche je Minute über alle Adressen; 0 = aus
    cookie_secure: bool = True


def load_auth_config(env: Mapping[str, str] | None = None) -> AuthConfig | None:
    """Zugangsdaten lesen; None = Anmeldung aus. Unbrauchbare Werte brechen den Start ab."""
    env = os.environ if env is None else env
    user = env.get("MAP_AUTH_USER", "").strip()
    password = env.get("MAP_AUTH_PASSWORD", "")
    required = env.get("MAP_AUTH_REQUIRED", "").lower() in {"1", "true", "yes"}
    if not password and not required:
        return None
    if not user or len(password) < 12:
        raise RuntimeError("MAP_AUTH_USER und MAP_AUTH_PASSWORD setzen (Kennwort mindestens 12 Zeichen)")
    secret = env.get("MAP_SESSION_SECRET", "").strip()
    if secret and len(secret) < 32:
        raise RuntimeError("MAP_SESSION_SECRET ist zu kurz (mindestens 32 Zeichen, z. B. openssl rand -hex 32)")
    if not secret:
        log.warning("MAP_SESSION_SECRET nicht gesetzt – Sitzungen enden bei jedem Neustart")
    return AuthConfig(
        user=user,
        password=password,
        secret=secret.encode("utf-8") if secret else secrets.token_bytes(32),
        session_hours=float(env.get("MAP_SESSION_HOURS", "12")),
        max_attempts=int(env.get("MAP_LOGIN_MAX_ATTEMPTS", "5")),
        lock_minutes=float(env.get("MAP_LOGIN_LOCK_MINUTES", "15")),
        global_max=int(env.get("MAP_LOGIN_GLOBAL_MAX", "30")),
        cookie_secure=env.get("MAP_COOKIE_SECURE", "true").strip().lower() not in _FALSE,
    )


def check_credentials(cfg: AuthConfig, user: str, password: str) -> bool:
    """Zeitkonstanter Vergleich beider Felder (verrät weder, welches falsch war, noch die Länge)."""
    ok_user = hmac.compare_digest(_digest(user), _digest(cfg.user))
    ok_pw = hmac.compare_digest(_digest(password), _digest(cfg.password))
    return ok_user and ok_pw


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _sign(cfg: AuthConfig, payload: str) -> str:
    return hmac.new(cfg.secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_token(cfg: AuthConfig, now: float | None = None) -> str:
    """Token = <Ablauf als Unix-Zeit>.<Zufall>.<HMAC>; enthält keine Nutzerdaten."""
    expires = int((time.time() if now is None else now) + cfg.session_hours * 3600)
    payload = f"{expires}.{secrets.token_hex(8)}"
    return f"{payload}.{_sign(cfg, payload)}"


def verify_session_token(cfg: AuthConfig, token: str | None, now: float | None = None) -> bool:
    """Signatur und Ablauf des Sitzungs-Tokens prüfen."""
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3 or not (parts[0].isascii() and parts[0].isdigit()):
        return False
    # als Bytes vergleichen: compare_digest wirft bei Nicht-ASCII-Text TypeError (manipulierter Cookie → 500)
    if not hmac.compare_digest(parts[2].encode("utf-8"), _sign(cfg, f"{parts[0]}.{parts[1]}").encode("utf-8")):
        return False
    return (time.time() if now is None else now) < int(parts[0])


def new_csrf_nonce() -> str:
    """Zufallswert für das CSRF-Cookie des Login-Formulars."""
    return secrets.token_hex(16)


def is_csrf_nonce(value: str | None) -> bool:
    """Nur selbst erzeugte Zufallswerte (32 Hex-Zeichen) werden weiterverwendet."""
    return value is not None and len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def csrf_token(cfg: AuthConfig, purpose: str, bound_to: str) -> str:
    """CSRF-Token = HMAC über Zweck und gebundenen Wert (Cookie-Zufallswert bzw. Sitzungs-Token)."""
    return _sign(cfg, f"csrf|{purpose}|{bound_to}")


def check_csrf(cfg: AuthConfig, purpose: str, bound_to: str | None, token: str | None) -> bool:
    """Zeitkonstanter Vergleich; fehlende Werte sind immer ungültig."""
    if not bound_to or not token:
        return False
    return hmac.compare_digest(token.encode("utf-8"), csrf_token(cfg, purpose, bound_to).encode("utf-8"))


@dataclass
class LoginLimiter:
    """Zählt Fehlversuche je Client-Adresse und sperrt vorübergehend. Im Speicher, je Prozess.

    Zusätzlich gilt ``global_max`` Fehlversuche je ``global_window`` Sekunden über alle Adressen: Wer die Adresse
    wechselt (gefälschtes X-Forwarded-For), bekommt dadurch keine weiteren Versuche. Die globale Grenze ist ein
    gleitendes Fenster; solange laufend mehr als ``global_max`` Fehlversuche je Minute eintreffen, ist die
    Anmeldung für alle gesperrt (bestehende Sitzungen bleiben gültig) – bewusst in Kauf genommen.
    """

    max_attempts: int = 5
    lock_seconds: float = 900.0
    global_max: int = 0
    global_window: float = 60.0
    _failures: dict[str, list[float]] = field(default_factory=dict)
    _locked_until: dict[str, float] = field(default_factory=dict)
    _recent: deque[float] = field(default_factory=deque)

    def is_locked(self, client: str, now: float | None = None) -> bool:
        """True, solange der Client gesperrt ist oder die globale Grenze erreicht ist."""
        now = time.time() if now is None else now
        while self._recent and now - self._recent[0] >= self.global_window:
            self._recent.popleft()
        if self.global_max and len(self._recent) >= self.global_max:
            return True
        until = self._locked_until.get(client)
        if until and now < until:
            return True
        if until:
            del self._locked_until[client]
            self._failures.pop(client, None)
        return False

    def record_failure(self, client: str, now: float | None = None) -> bool:
        """True, wenn der Client jetzt gesperrt ist."""
        now = time.time() if now is None else now
        if len(self._locked_until) > 1024:  # abgelaufene Sperren wechselnder Adressen nicht ewig halten
            for key in [k for k, until in self._locked_until.items() if until <= now]:
                del self._locked_until[key]
                self._failures.pop(key, None)
        self._recent.append(now)
        if len(self._recent) > max(self.global_max, 1) * 2:  # Speicher begrenzen
            self._recent.popleft()
        window = [t for t in self._failures.get(client, []) if now - t < self.lock_seconds] + [now]
        self._failures[client] = window
        if len(window) >= self.max_attempts:
            self._locked_until[client] = now + self.lock_seconds
            log.warning("Anmeldung für %s nach %s Fehlversuchen gesperrt", client, len(window))
            return True
        if len(self._failures) > 10_000:  # Speicher begrenzen
            for c in sorted(self._failures, key=lambda c: self._failures[c][-1])[:5000]:
                self._failures.pop(c, None)
        return False

    def reset(self, client: str) -> None:
        """Zähler nach erfolgreicher Anmeldung löschen."""
        self._failures.pop(client, None)
        self._locked_until.pop(client, None)
