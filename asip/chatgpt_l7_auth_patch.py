# -*- coding: utf-8 -*-
"""chatgpt_l7 multi-account 429 fallback patch.

429 escalation ladder:
  1. Try swap IP (existing logic, need_new_ip=True)
  2. Still 429 → try switch auth mode (acct1 ⇄ acct2 ⇄ no-login)
  3. All 3 modes exhausted → fallback to backoff window

Auth modes:
  - acct1: default account (CHATGPT_ACCT1_COOKIE / CHATGPT_ACCT1_AUTH_HEADER)
  - acct2: backup account (CHATGPT_ACCT2_COOKIE / CHATGPT_ACCT2_AUTH_HEADER)
  - nologin: no auth headers (public/anonymous requests)

Env vars (add to .env):
  CHATGPT_ACCT1_COOKIE="__Secure-next-auth.session-token=..."
  CHATGPT_ACCT1_AUTH_HEADER="Bearer sess-..."
  CHATGPT_ACCT2_COOKIE="__Secure-next-auth.session-token=..."
  CHATGPT_ACCT2_AUTH_HEADER="Bearer sess-..."

Inject point:
  - _Handler._build_upstream_head() override to inject Cookie/Authorization
  - Before _note_429() → try _switch_auth_mode()
  - Track _CURRENT_AUTH_MODE ("acct1" | "acct2" | "nologin")
"""
import os
import time
import threading

# Auth pool config
AUTH_MODES = ("acct1", "acct2", "nologin")
_CURRENT_AUTH_MODE = "acct1"   # global state
_AUTH_LAST_SWITCH = 0.0
_AUTH_SWITCH_COOLDOWN = 5.0    # 5s cooldown (prevent rapid cycling)
_AUTH_LOCK = threading.Lock()
_AUTH_STATS = {"switches": 0, "acct1_uses": 0, "acct2_uses": 0, "nologin_uses": 0}


def _load_auth_pool():
    """Load auth credentials from env."""
    return {
        "acct1": {
            "cookie": os.getenv("CHATGPT_ACCT1_COOKIE", ""),
            "auth_header": os.getenv("CHATGPT_ACCT1_AUTH_HEADER", ""),
        },
        "acct2": {
            "cookie": os.getenv("CHATGPT_ACCT2_COOKIE", ""),
            "auth_header": os.getenv("CHATGPT_ACCT2_AUTH_HEADER", ""),
        },
        "nologin": {
            "cookie": "",
            "auth_header": "",
        },
    }


_AUTH_POOL = _load_auth_pool()


def current_auth_mode():
    """Get current auth mode (for status endpoint)."""
    return _CURRENT_AUTH_MODE


def current_auth_creds():
    """Return (cookie, auth_header) for current mode."""
    creds = _AUTH_POOL.get(_CURRENT_AUTH_MODE, _AUTH_POOL["nologin"])
    return creds.get("cookie", ""), creds.get("auth_header", "")


def switch_auth_mode():
    """Switch to next auth mode. Returns dict."""
    global _CURRENT_AUTH_MODE, _AUTH_LAST_SWITCH
    now = time.time()
    with _AUTH_LOCK:
        if now - _AUTH_LAST_SWITCH < _AUTH_SWITCH_COOLDOWN:
            return {"switched": False, "mode": _CURRENT_AUTH_MODE,
                    "reason": "auth_cooldown"}
        old = _CURRENT_AUTH_MODE
        idx = AUTH_MODES.index(old)
        new_idx = (idx + 1) % len(AUTH_MODES)
        new_mode = AUTH_MODES[new_idx]
        _CURRENT_AUTH_MODE = new_mode
        _AUTH_LAST_SWITCH = now
        _AUTH_STATS["switches"] += 1
        _AUTH_STATS[f"{new_mode}_uses"] += 1
    return {"switched": True, "mode": new_mode, "prev": old, "reason": "ok"}


def auth_stats():
    """Return auth stats for status endpoint."""
    return dict(_AUTH_STATS)


# Integration note: chatgpt_l7._Handler must call these functions
