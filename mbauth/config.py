from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class AuthConfig:
    """Browser login. Machine clients still use WEB_TOKEN / X-Shot-Token."""

    mode: str = "off"  # off | local | ldap
    session_secret: str = ""
    session_ttl_hours: int = 12
    cookie_name: str = "mb_session"
    # local: "user:pass,user2:pass2"
    users: dict[str, str] = field(default_factory=dict)
    # ldap
    ldap_url: str = ""
    ldap_base_dn: str = ""
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    ldap_user_filter: str = "(sAMAccountName={username})"
    ldap_user_dn_template: str = ""  # e.g. uid={username},ou=people,dc=example,dc=com
    ldap_start_tls: bool = False
    ldap_require_group: str = ""
    ldap_timeout_sec: int = 8
    brand: str = "ShotCore"

    @property
    def enabled(self) -> bool:
        return self.mode in {"local", "ldap"} and bool(self.session_secret)

    def resolve_api_token(self, explicit: str = "") -> str:
        """Service token accepted as X-Shot-Token / ?token= (ShotCore UI + machine clients)."""
        for candidate in (
            (explicit or "").strip(),
            (os.getenv("WEB_TOKEN") or "").strip(),
            (os.getenv("AUTH_API_TOKEN") or "").strip(),
        ):
            if candidate:
                return candidate
        if self.enabled and self.session_secret and self.session_secret != "mbauth-dev-change-me":
            return self.session_secret
        return ""

    def resolve_ui_token(self, explicit: str = "") -> str:
        """Token that locks the browser UI. ShotTrader: only TRADER_TOKEN / UI_TOKEN — not SHOTCORE_TOKEN."""
        for candidate in (
            (explicit or "").strip(),
            (os.getenv("TRADER_TOKEN") or "").strip(),
            (os.getenv("UI_TOKEN") or "").strip(),
        ):
            if candidate:
                return candidate
        # ShotCore keeps using WEB_TOKEN for its own page when AUTH_MODE=off
        if self.brand == "ShotCore":
            return self.resolve_api_token(explicit)
        return ""


def _parse_users(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        user, _, password = part.partition(":")
        user = user.strip()
        password = password.strip()
        if user and password:
            out[user] = password
    return out


def load_auth_config(*, brand: str = "ShotCore", token_fallback: str = "") -> AuthConfig:
    mode = (os.getenv("AUTH_MODE") or "off").strip().lower()
    if mode in {"0", "false", "no", "none", ""}:
        mode = "off"
    if mode not in {"off", "local", "ldap"}:
        mode = "off"

    secret = (os.getenv("SESSION_SECRET") or "").strip()
    if not secret and mode != "off":
        # Stable fallback so restarts keep sessions if WEB_TOKEN is set.
        secret = (token_fallback or os.getenv("WEB_TOKEN") or "").strip()
    if not secret and mode != "off":
        # Last resort — sessions die on restart but auth still works.
        secret = "mbauth-dev-change-me"

    return AuthConfig(
        mode=mode,
        session_secret=secret,
        session_ttl_hours=max(1, int(float(os.getenv("SESSION_TTL_HOURS") or "12"))),
        cookie_name=(os.getenv("SESSION_COOKIE") or "mb_session").strip() or "mb_session",
        users=_parse_users(os.getenv("AUTH_USERS") or ""),
        ldap_url=(os.getenv("LDAP_URL") or "").strip(),
        ldap_base_dn=(os.getenv("LDAP_BASE_DN") or "").strip(),
        ldap_bind_dn=(os.getenv("LDAP_BIND_DN") or "").strip(),
        ldap_bind_password=os.getenv("LDAP_BIND_PASSWORD") or "",
        ldap_user_filter=(os.getenv("LDAP_USER_FILTER") or "(sAMAccountName={username})").strip(),
        ldap_user_dn_template=(os.getenv("LDAP_USER_DN_TEMPLATE") or "").strip(),
        ldap_start_tls=(os.getenv("LDAP_START_TLS") or "").strip().lower() in {"1", "true", "yes", "on"},
        ldap_require_group=(os.getenv("LDAP_REQUIRE_GROUP") or "").strip(),
        ldap_timeout_sec=max(2, int(float(os.getenv("LDAP_TIMEOUT_SEC") or "8"))),
        brand=brand,
    )
