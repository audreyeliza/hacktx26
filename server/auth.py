"""Demo-grade login (AGENTS.md §19): accounts from config.yaml, in-memory sessions, a session
cookie and role checks.

DEMO ONLY: plaintext demo passwords in config, sessions lost on restart, no lockout, no CSRF
tokens (relies on SameSite=Lax), plain HTTP. Never use real credentials.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from fastapi import HTTPException, Request, WebSocket

from server.config import AuthCfg

ROLES = ("nurse", "physician", "admin")
CAN_EDIT_PATIENTS = ("nurse", "admin")
ADMIN = ("admin",)


def public_user(account: Any) -> dict[str, str]:
    return {"username": account.username, "display_name": account.display_name, "role": account.role}


class Sessions:
    def __init__(self, cfg: AuthCfg) -> None:
        self.cfg = cfg
        self._sessions: dict[str, tuple[dict[str, str], float]] = {}

    # ---- accounts
    def check_password(self, username: str, password: str) -> dict[str, str] | None:
        for acc in self.cfg.accounts:
            if acc.username == username and secrets.compare_digest(acc.password, password):
                return public_user(acc)
        return None

    def account_for_role(self, role: str) -> dict[str, str] | None:
        return next((public_user(a) for a in self.cfg.accounts if a.role == role), None)

    def demo_accounts(self) -> list[dict[str, str]]:
        seen: dict[str, dict[str, str]] = {}
        for a in self.cfg.accounts:
            seen.setdefault(a.role, {"role": a.role, "display_name": a.display_name})
        return [seen[r] for r in ROLES if r in seen]

    # ---- sessions
    def create(self, user: dict[str, str]) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = (user, time.time() + self.cfg.session_ttl_h * 3600)
        return token

    def get(self, token: str | None) -> dict[str, str] | None:
        if not token or token not in self._sessions:
            return None
        user, expires = self._sessions[token]
        if time.time() > expires:
            del self._sessions[token]
            return None
        return user

    def delete(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    def user_for(self, cookies: dict[str, str]) -> dict[str, str] | None:
        if not self.cfg.enabled:
            return self.account_for_role("admin")
        return self.get(cookies.get(self.cfg.cookie_name))

    # ---- FastAPI helpers
    def require(self, request: Request, roles: tuple[str, ...] = ROLES) -> dict[str, str]:
        user = self.user_for(request.cookies)
        if user is None:
            raise HTTPException(401, "not signed in")
        if user["role"] not in roles:
            raise HTTPException(403, f"role {user['role']} cannot do this")
        return user

    async def require_ws(self, ws: WebSocket) -> dict[str, str] | None:
        """For WebSockets: accept, then close with 4401 if not signed in (§19.2)."""
        user = self.user_for(ws.cookies)
        if user is None:
            await ws.accept()
            await ws.close(code=4401)
        return user
