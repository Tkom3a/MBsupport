from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import AuthConfig

log = logging.getLogger("mbauth")


@dataclass
class AuthResult:
    ok: bool
    username: str = ""
    error: str = ""


def verify_local(cfg: AuthConfig, username: str, password: str) -> AuthResult:
    user = (username or "").strip()
    if not user or not password:
        return AuthResult(False, error="Укажите логин и пароль")
    expected = cfg.users.get(user)
    if expected is None or expected != password:
        return AuthResult(False, error="Неверный логин или пароль")
    return AuthResult(True, username=user)


def _member_of(entry) -> list[str]:
    if not hasattr(entry, "memberOf"):
        return []
    raw = entry.memberOf.value
    if isinstance(raw, list):
        return [str(g) for g in raw]
    if raw:
        return [str(raw)]
    return []


def _in_group(groups: list[str], require: str) -> bool:
    needle = require.lower()
    return any(needle in g.lower() for g in groups)


def _open_conn(server, *, user: str | None = None, password: str | None = None, start_tls: bool = False, timeout: int = 8):
    from ldap3 import Connection

    conn = Connection(
        server,
        user=user,
        password=password,
        auto_bind=False,
        receive_timeout=timeout,
    )
    conn.open()
    if start_tls:
        conn.start_tls()
    if user is not None:
        if not conn.bind():
            conn.unbind()
            return None
    else:
        # anonymous
        conn.bind()
    return conn


def verify_ldap(cfg: AuthConfig, username: str, password: str) -> AuthResult:
    user = (username or "").strip()
    if not user or not password:
        return AuthResult(False, error="Укажите логин и пароль")
    if not cfg.ldap_url:
        return AuthResult(False, error="LDAP_URL не задан")

    try:
        from ldap3 import ALL, Server
        from ldap3.core.exceptions import LDAPException
    except ImportError:
        return AuthResult(False, error="Пакет ldap3 не установлен")

    try:
        server = Server(cfg.ldap_url, get_info=ALL, connect_timeout=cfg.ldap_timeout_sec)
        groups: list[str] = []

        if cfg.ldap_user_dn_template:
            user_dn = cfg.ldap_user_dn_template.format(username=user)
        else:
            if not cfg.ldap_base_dn:
                return AuthResult(False, error="Задайте LDAP_BASE_DN или LDAP_USER_DN_TEMPLATE")
            search_user = cfg.ldap_bind_dn or None
            search_pass = cfg.ldap_bind_password if cfg.ldap_bind_dn else None
            search_conn = _open_conn(
                server,
                user=search_user,
                password=search_pass,
                start_tls=cfg.ldap_start_tls,
                timeout=cfg.ldap_timeout_sec,
            )
            if search_conn is None and cfg.ldap_bind_dn:
                return AuthResult(False, error="Не удалось подключиться к LDAP (bind)")
            if search_conn is None:
                return AuthResult(False, error="Не удалось подключиться к LDAP")
            filt = cfg.ldap_user_filter.format(username=user)
            ok = search_conn.search(
                search_base=cfg.ldap_base_dn,
                search_filter=filt,
                attributes=["memberOf"],
                size_limit=1,
            )
            if not ok or not search_conn.entries:
                search_conn.unbind()
                return AuthResult(False, error="Неверный логин или пароль")
            entry = search_conn.entries[0]
            user_dn = str(entry.entry_dn)
            groups = _member_of(entry)
            search_conn.unbind()
            if cfg.ldap_require_group and not _in_group(groups, cfg.ldap_require_group):
                return AuthResult(False, error="Нет доступа (группа LDAP)")

        bind_conn = _open_conn(
            server,
            user=user_dn,
            password=password,
            start_tls=cfg.ldap_start_tls,
            timeout=cfg.ldap_timeout_sec,
        )
        if bind_conn is None:
            return AuthResult(False, error="Неверный логин или пароль")
        if cfg.ldap_require_group and cfg.ldap_user_dn_template:
            bind_conn.search(
                search_base=user_dn,
                search_filter="(objectClass=*)",
                attributes=["memberOf"],
                size_limit=1,
            )
            if bind_conn.entries and not _in_group(_member_of(bind_conn.entries[0]), cfg.ldap_require_group):
                bind_conn.unbind()
                return AuthResult(False, error="Нет доступа (группа LDAP)")
        bind_conn.unbind()
        return AuthResult(True, username=user)
    except LDAPException as exc:
        log.warning("LDAP auth failed for %s: %s", user, exc)
        return AuthResult(False, error="Неверный логин или пароль")
    except Exception as exc:
        log.exception("LDAP error")
        return AuthResult(False, error=f"Ошибка LDAP: {exc}")


def authenticate(cfg: AuthConfig, username: str, password: str) -> AuthResult:
    if cfg.mode == "local":
        if not cfg.users:
            return AuthResult(False, error="AUTH_USERS пуст")
        return verify_local(cfg, username, password)
    if cfg.mode == "ldap":
        return verify_ldap(cfg, username, password)
    return AuthResult(False, error="Авторизация выключена")
