"""LLM summaries (AGENTS.md §10) with Google Gemini.

The LLM only explains and summarizes what the deterministic rules already produced; nothing it
writes feeds back into alerts (design rule 1). Input is a structured JSON digest built here from
stored vitals, events and alerts — never raw waveforms — plus, for the overnight summary and
the SBAR handoff, a PNG of the breathing-rate trend that Gemini is asked to describe and
cross-check against the digest. Every response is cached; if the API fails (no network, bad
key, timeout) the latest cached response is returned with `cached: true`.

The digest leaves out the patient's name (bed number only) and says whether the data is simulated.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from pydantic import BaseModel

from server.config import ROOT, Config
from server.store import Store

log = logging.getLogger(__name__)

KINDS = ("explain", "overnight", "handoff")

SYSTEM_PROMPT = """You write short clinical summaries for nurses on a general hospital ward, from data produced by \
BreathWatch, a research prototype breathing monitor (not a medical device; its thresholds are demonstration values).

Rules:
- Use only the data provided: the JSON digest and, when attached, the breathing-rate chart image. Do not invent \
values, times, events or history.
- State uncertainty plainly (signal quality, single sensor, short monitoring time, simulated data).
- No diagnoses. No medication, dosing or treatment advice.
- Recommendations are limited to monitoring and clinician-review suggestions (for example: bedside check of the \
patient and sensor placement, closer observation, ask the responsible clinician to review).
- Alert tiers were set by fixed rules; explain them, do not change or second-guess them.
- If the digest says the data is simulated, say so.
- Mention once that BreathWatch is a research prototype.
- When a chart image is attached: first describe the pattern you see in it, then cross-check it against the digest. \
If the chart and the digest disagree, report the disagreement; do not resolve it by guessing.
- Plain language, short sentences, no markdown tables."""

TASKS = {
    "explain": "Explain this alert to the bedside nurse in at most 120 words: what was detected, why the rules "
               "raised this tier, how confident the signal was, and what to check. Digest:",
    "overnight": "Write an overnight summary in at most 250 words with three short parts, labelled "
                 "'Chart:', 'Summary:' and 'Discrepancies:' (write 'none' if the chart and digest agree). Digest:",
    "handoff": "Write an SBAR handoff note. Keep each section to 1-3 sentences. 'recommendation' may only suggest "
               "monitoring or clinician review. Also fill 'chart_observation' (what the chart shows) and "
               "'discrepancies' (chart vs digest; empty list if none). Digest:",
}


class SBAR(BaseModel):
    situation: str
    background: str
    assessment: str
    recommendation: str
    chart_observation: str
    discrepancies: list[str]


class LLMError(Exception):
    """The provider could not produce a summary (no key, network, timeout, bad response)."""


class LLMUnavailable(Exception):
    """The provider failed and nothing is cached yet."""


class Provider(Protocol):
    name: str

    def summarize(self, kind: str, digest: dict[str, Any], images: list[bytes]) -> str: ...


# --------------------------------------------------------------------------- providers

def load_api_key(env_name: str, dotenv_path: Path | None = None) -> tuple[str | None, str]:
    """(key, where) — the environment variable wins over the repo-root .env file. The key itself
    is never logged; only `where` ("environment", ".env" or "missing") is."""
    key = os.environ.get(env_name)
    if key:
        return key, "environment"
    from dotenv import dotenv_values
    path = dotenv_path or ROOT / ".env"
    key = dotenv_values(path).get(env_name) if path.exists() else None
    return (key, ".env") if key else (None, "missing")


class GeminiProvider:
    """Google Gemini via the official google-genai SDK. The key comes from the environment."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg.llm
        self.name = "gemini"
        self._client: Any = None
        self.last_model: str | None = None   # the model that actually answered

    def model_for(self, kind: str) -> str:
        return self.cfg.handoff_model if kind == "handoff" else self.cfg.model

    def models_for(self, kind: str) -> list[str]:
        first = self.model_for(kind)
        return [first] + [m for m in self.cfg.fallback_models if m != first]

    def _get_client(self) -> Any:
        if self._client is None:
            key, where = load_api_key(self.cfg.api_key_env)
            if not key:
                raise LLMError(f"{self.cfg.api_key_env} is not set (environment or .env)")
            log.info("Gemini key loaded from %s", where)
            from google import genai
            from google.genai import types
            self._client = genai.Client(api_key=key, http_options=types.HttpOptions(
                timeout=int(self.cfg.timeout_s * 1000),
                retry_options=types.HttpRetryOptions(attempts=self.cfg.attempts_per_model, initial_delay=0.5,
                                                     max_delay=2.0)))
        return self._client

    def summarize(self, kind: str, digest: dict[str, Any], images: list[bytes]) -> str:
        from google.genai import types
        client = self._get_client()
        parts: list[Any] = [TASKS[kind] + "\n" + json.dumps(digest, indent=1)]
        parts += [types.Part.from_bytes(data=img, mime_type="image/png") for img in images]
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT, temperature=self.cfg.temperature,
            max_output_tokens=self.cfg.max_output_tokens,
            **({"response_mime_type": "application/json", "response_schema": SBAR} if kind == "handoff" else {}),
        )
        failures: list[str] = []
        deadline = time.monotonic() + self.cfg.total_timeout_s - 1.0
        for model in self.models_for(kind):
            remaining = deadline - time.monotonic()
            if remaining < 2.0:
                failures.append(f"{model}: skipped (out of time)")
                break
            config.http_options = types.HttpOptions(timeout=int(min(self.cfg.timeout_s, remaining) * 1000))
            try:
                response = client.models.generate_content(model=model, contents=parts, config=config)
            except Exception as exc:   # network, auth, quota, timeout: the caller falls back to the cache
                msg = _redact(f"{model}: {type(exc).__name__}: {exc}", self.cfg.api_key_env)
                failures.append(msg)
                if _overloaded(exc):
                    log.warning("Gemini %s busy, trying the next model", model)
                    continue
                raise LLMError(msg) from exc
            text = (response.text or "").strip()
            if not text:
                failures.append(f"{model}: empty response")
                continue
            self.last_model = model
            return text
        raise LLMError(" | ".join(failures))


class FakeProvider:
    """Deterministic stand-in for tests and offline development (`llm.provider: fake`)."""

    def __init__(self, cfg: Config | None = None, fail: bool = False) -> None:
        self.name = "fake"
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any], list[bytes]]] = []

    def model_for(self, kind: str) -> str:
        return "fake-model"

    def summarize(self, kind: str, digest: dict[str, Any], images: list[bytes]) -> str:
        self.calls.append((kind, digest, images))
        if self.fail:
            raise LLMError("simulated outage")
        if kind == "handoff":
            return json.dumps({"situation": f"Bed {digest['patient']['bed']}: {len(digest['alerts'])} alerts.",
                               "background": "Prototype data.", "assessment": "See digest.",
                               "recommendation": "Bedside check; clinician review.",
                               "chart_observation": "Trend shown.", "discrepancies": []})
        return f"[{kind}] Bed {digest['patient']['bed']}: research prototype summary."


def _overloaded(exc: Exception) -> bool:
    """Server busy (5xx) or rate limited (429): worth trying another model. Auth/bad-request is not."""
    code = getattr(exc, "code", None)
    return isinstance(code, int) and (code >= 500 or code == 429)


def _redact(text: str, env_name: str) -> str:
    """Belt and braces: never let the key reach a log line or an HTTP error detail."""
    key, _ = load_api_key(env_name)
    return text.replace(key, "[redacted]") if key else text


def make_provider(cfg: Config) -> Provider:
    if cfg.llm.provider == "fake":
        return FakeProvider(cfg)
    return GeminiProvider(cfg)


# --------------------------------------------------------------------------- digest

def _clock(t: float | None) -> str | None:
    return None if t is None else time.strftime("%H:%M", time.localtime(t))


def _stats(values: list[float]) -> dict[str, Any] | None:
    if not values:
        return None
    a = np.array(values, dtype=float)
    return {"n": len(a), "mean": round(float(a.mean()), 1), "min": round(float(a.min()), 1),
            "max": round(float(a.max()), 1), "latest": round(float(a[-1]), 1)}


def build_digest(cfg: Config, store: Store, patient: dict[str, Any], now_t: float, kind: str,
                 baseline: dict[str, Any] | None = None, alert: dict[str, Any] | None = None) -> dict[str, Any]:
    """Structured summary of stored data for one patient (never raw waveforms)."""
    lc = cfg.llm
    pid = patient["id"]
    if kind == "explain" and alert is not None:
        since = alert["raised_t"] - lc.explain_window_s
        until = alert["raised_t"] + 60
    else:
        since = max(now_t - lc.overnight_window_s, patient.get("admitted_t") or 0)
        until = now_t
    vitals = [v for v in store.vitals(pid, since, limit=100000) if v.get("t") is None or v["t"] <= until]
    rr = [v["rr_fused"] for v in vitals if v.get("rr_fused") is not None]
    hr = [v["hr"] for v in vitals if v.get("hr") is not None]
    quality = Counter(v.get("quality") or "unknown" for v in vitals)
    posture = Counter(v.get("posture") or "unknown" for v in vitals)
    flags = Counter(f for v in vitals for f in (v.get("flags") or []) if not f.startswith("outlier"))
    n = max(len(vitals), 1)
    quarter = max(len(rr) // 4, 1)
    events = [e for e in store.events(pid, since, limit=1000) if e["start_t"] <= until]
    alerts = [a for a in store.alerts(pid, "all", since, limit=1000) if a["tier"] >= 1 and a["raised_t"] <= until]
    dev = patient["device"]
    digest: dict[str, Any] = {
        "patient": {"bed": patient["bed"], "risk_factors": patient["risk_factors"],
                    "notes": (patient.get("notes") or "")[:lc.notes_max_chars]},
        "data_source": ("SIMULATED data (demo scenario)" if dev["kind"] == "simulator"
                        else "live prototype wearable"),
        "time_range": {"from": _clock(vitals[0]["t"] if vitals else since), "to": _clock(vitals[-1]["t"] if vitals else until),
                       "minutes": round(((vitals[-1]["t"] - vitals[0]["t"]) / 60) if len(vitals) > 1 else 0, 1)},
        "breathing_rate_per_min": {
            **({"stats": _stats(rr)} if rr else {"stats": None}),
            "baseline": baseline["rr"] if baseline else None,
            "first_quarter_mean": round(float(np.mean(rr[:quarter])), 1) if rr else None,
            "last_quarter_mean": round(float(np.mean(rr[-quarter:])), 1) if rr else None,
            "minutes_without_reliable_rate": round((n - len(rr)) * cfg.signals.rate_interval_s / 60, 1),
        },
        "heart_rate_per_min": _stats(hr),
        "signal_quality_fraction": {k: round(c / n, 2) for k, c in quality.most_common()},
        "rate_flags_fraction": {k: round(c / n, 2) for k, c in flags.most_common()},
        "posture_fraction": {k: round(c / n, 2) for k, c in posture.most_common()},
        "events": {
            "count_by_type": dict(Counter(e["type"] for e in events)),
            "count_by_posture": dict(Counter(e["posture"] for e in events)),
            "longest_s": max((e["duration_s"] for e in events), default=None),
            "recent": [{"type": e["type"], "at": _clock(e["start_t"]), "duration_s": e["duration_s"],
                        "posture": e["posture"]} for e in events[:lc.max_events_listed]],
            "note": "hypopnea is simplified: SpO2 is not measured",
        },
        "alerts": [{"tier": a["tier"], "kind": a["kind"], "status": a["status"], "at": _clock(a["raised_t"]),
                    "reason": a["reason"], "escalated": a["escalated"],
                    "acknowledged_by_role": (a["acknowledged_by"] or {}).get("role")}
                   for a in alerts[:lc.max_alerts_listed]],
    }
    if alert is not None:
        digest["alert_to_explain"] = {"tier": alert["tier"], "kind": alert["kind"], "reason": alert["reason"],
                                      "status": alert["status"], "at": _clock(alert["raised_t"])}
    return digest


# --------------------------------------------------------------------------- chart

EVENT_COLORS = {"obstructive": "#e07b24", "central": "#3b6ea8", "mixed": "#8e5ea2", "hypopnea": "#2a9d8f"}


def render_chart(cfg: Config, store: Store, patient: dict[str, Any], now_t: float,
                 baseline: dict[str, Any] | None = None) -> bytes:
    """Small PNG: breathing-rate trend, baseline, demo thresholds, events and alerts (tier >= 2)."""
    from matplotlib.figure import Figure   # Figure (not pyplot) is safe off the main thread

    pid = patient["id"]
    since = max(now_t - cfg.llm.overnight_window_s, patient.get("admitted_t") or 0)
    vitals = store.vitals(pid, since, limit=100000)
    events = store.events(pid, since, limit=1000)
    alerts = [a for a in store.alerts(pid, "all", since, limit=1000) if a["tier"] >= 2]
    t0 = vitals[0]["t"] if vitals else now_t
    to_min = lambda t: (t - t0) / 60.0

    fig = Figure(figsize=(7.0, 2.8), dpi=110)
    ax = fig.add_subplot(1, 1, 1)
    xs = [to_min(v["t"]) for v in vitals]
    ys = [np.nan if v.get("rr_fused") is None else v["rr_fused"] for v in vitals]
    ax.plot(xs, ys, color="#1f2933", linewidth=1.3, label="breathing rate")
    if baseline:
        ax.axhline(baseline["rr"], color="#6b7785", linestyle="--", linewidth=1, label=f"baseline {baseline['rr']:.0f}")
    for thr in (cfg.alerts.rr_tier1_below, cfg.alerts.rr_tier2_below, cfg.alerts.rr_tier3_below):
        ax.axhline(thr, color="#c62828", alpha=0.18, linewidth=0.8)
    seen: set[str] = set()
    for e in events:
        ax.axvspan(to_min(e["start_t"]), to_min(e["end_t"]), color=EVENT_COLORS.get(e["type"], "#999"), alpha=0.35,
                   label=None if e["type"] in seen else e["type"])
        seen.add(e["type"])
    for a in alerts:
        ax.plot(to_min(a["raised_t"]), 1.5, marker="^", color="#c62828" if a["tier"] == 3 else "#e07b24",
                markersize=7, linestyle="none")
    finite = [y for y in ys if np.isfinite(y)]
    ax.set_ylim(0, max(25.0, max(finite) + 3) if finite else 25.0)
    ax.set_xlabel(f"minutes since {_clock(t0)} (triangles: alerts tier 2-3)")
    ax.set_ylabel("breaths/min")
    ax.set_title(f"Bed {patient['bed']} — breathing rate (BreathWatch prototype)", fontsize=10)
    ax.legend(fontsize=7, loc="upper right", ncol=3)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()


# --------------------------------------------------------------------------- service

class Summarizer:
    def __init__(self, cfg: Config, store: Store, provider: Provider | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.provider = provider or make_provider(cfg)

    async def summarize(self, kind: str, patient: dict[str, Any], now_t: float,
                        baseline: dict[str, Any] | None = None, alert: dict[str, Any] | None = None) -> dict[str, Any]:
        """A §19.3 Summary. Raises LLMUnavailable if the provider fails and nothing is cached."""
        assert kind in KINDS
        pid = patient["id"]
        key = str(alert["id"]) if alert is not None else ""
        digest = build_digest(self.cfg, self.store, patient, now_t, kind, baseline, alert)
        images: list[bytes] = []
        if kind in ("overnight", "handoff"):
            images = [await asyncio.to_thread(render_chart, self.cfg, self.store, patient, now_t, baseline)]
        try:
            text = await asyncio.wait_for(asyncio.to_thread(self.provider.summarize, kind, digest, images),
                                          timeout=self.cfg.llm.total_timeout_s)
            summary = self._shape(kind, pid, alert, text)
        except (LLMError, asyncio.TimeoutError, ValueError) as exc:
            if isinstance(exc, asyncio.TimeoutError):
                exc = LLMError(f"no answer within {self.cfg.llm.total_timeout_s:.0f} s")
            log.warning("LLM %s for %s failed (%s); using cache", kind, pid, exc)
            cached = self.store.cached_summary(pid, kind, key if kind == "explain" else None)
            if cached is None:
                raise LLMUnavailable(f"Gemini unavailable ({exc}) and no cached {kind} summary yet") from exc
            return {**cached, "cached": True}
        self.store.cache_summary(pid, kind, key, summary)
        return summary

    def _shape(self, kind: str, pid: str, alert: dict[str, Any] | None, text: str) -> dict[str, Any]:
        sbar = None
        if kind == "handoff":
            data = SBAR.model_validate_json(_strip_fences(text))   # ValueError on a malformed reply
            sbar = {k: getattr(data, k) for k in ("situation", "background", "assessment", "recommendation")}
            disc = "; ".join(data.discrepancies) or "none"
            text = (f"Situation: {data.situation}\nBackground: {data.background}\nAssessment: {data.assessment}\n"
                    f"Recommendation: {data.recommendation}\nChart: {data.chart_observation}\n"
                    f"Chart vs data discrepancies: {disc}")
        model = getattr(self.provider, "last_model", None) or (
            self.provider.model_for(kind) if hasattr(self.provider, "model_for") else self.provider.name)
        return {"kind": kind, "patient_id": pid, "alert_id": alert["id"] if alert else None, "text": text,
                "sbar": sbar, "model": model, "cached": False, "created_t": time.time()}


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()
