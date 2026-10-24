"""PUBLIC_DEMO mode (AGENTS.md §20): the app on the public internet with simulated data only.

1. On with env PUBLIC_DEMO=1 (or public_demo.enabled in config.yaml).
2. Simulated beds only: no UDP or serial listener; the live bed becomes a looping simulator.
3. Admin locked: source switching, serial ports and recording off; demo reset rate-limited.
4. Login: demo-role sign-in buttons only (password login off).
5. Gemini cost control: per-session and global summary limits; past them, cached summaries.
6. Banner "Public demo — simulated data, research prototype" on every page.
7. Bound to localhost behind an HTTPS reverse proxy (Caddy), proxy headers trusted from it.
"""

from __future__ import annotations

import html
import os
import time
from collections import defaultdict, deque
from typing import Any

from server.config import Config

TRUE = {"1", "true", "yes", "on"}


def is_enabled(cfg: Config) -> bool:
    return cfg.public_demo.enabled or os.environ.get("PUBLIC_DEMO", "").strip().lower() in TRUE


def simulated_device(cfg: Config) -> dict[str, Any]:
    return {"kind": "simulator", "scenario": cfg.public_demo.live_bed_scenario, "speed": 1.0, "loop": True}


class RateLimiter:
    """Sliding-window counters (in memory; a restart resets them)."""

    def __init__(self) -> None:
        self._hits: dict[Any, deque[float]] = defaultdict(deque)

    def _window(self, key: Any, window_s: float, now: float) -> deque[float]:
        hits = self._hits[key]
        while hits and hits[0] <= now - window_s:
            hits.popleft()
        return hits

    def check(self, limits: list[tuple[Any, int, float]], now: float | None = None) -> bool:
        """True (and counted against every limit) only if all limits have room."""
        now = time.time() if now is None else now
        windows = [(self._window(key, window_s, now), limit) for key, limit, window_s in limits]
        if any(len(hits) >= limit for hits, limit in windows):
            return False
        for hits, _ in windows:
            hits.append(now)
        return True

    def retry_after(self, key: Any, window_s: float, now: float | None = None) -> float:
        now = time.time() if now is None else now
        hits = self._window(key, window_s, now)
        return max(0.0, hits[0] + window_s - now) if hits else 0.0


def banner_html(text: str) -> str:
    return ('<div class="public-demo-banner" role="note" style="background:#fff4d6;color:#4a3b00;'
            'text-align:center;padding:6px 12px;font:14px/1.4 system-ui,sans-serif;'
            'border-bottom:1px solid #e6d28a">' + html.escape(text) + '</div>')


def inject_banner(page: str, text: str) -> str:
    """Insert the banner right after <body ...>."""
    i = page.find("<body")
    j = page.find(">", i) if i >= 0 else -1
    if j < 0:
        return banner_html(text) + page
    return page[:j + 1] + "\n" + banner_html(text) + page[j + 1:]
