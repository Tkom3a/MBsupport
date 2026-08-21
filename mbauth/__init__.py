"""Shared browser auth for ShotCore / ShotTrader (local users or LDAP)."""

from .config import AuthConfig, load_auth_config
from .web import attach_auth, auth_enabled

__all__ = ["AuthConfig", "load_auth_config", "attach_auth", "auth_enabled"]
