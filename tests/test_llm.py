"""Milestone 7: Gemini summaries — digest, chart, cache fallback, endpoints. No network: a fake
provider stands in for Gemini (and GeminiProvider is only exercised without a key)."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.llm import (SYSTEM_PROMPT, FakeProvider, GeminiProvider, LLMUnavailable, Summarizer, build_digest,
                        render_chart)
from server.store import Store
from tests.conftest import with_overrides
from tests.helpers import run_pipeline_full

SUMMARY = {"kind", "patient_id", "alert_id", "text", "sbar", "model", "cached", "created_t"}
SBAR_KEYS = {"situation", "background", "assessment", "recommendation"}
PATIENT = {"id": "p-test", "name": "Jordan Avery", "bed": "4", "notes": "Post-op day 1",
           "risk_factors": {"opioids": True, "supplemental_o2": True, "osa": "suspected", "age_over_65": False},
           "device": {"kind": "simulator", "scenario": "demo"}, "status": "admitted", "admitted_t": 0.0,
           "discharged_t": None}


@pytest.fixture(scope="module")
def demo_store(cfg):
    store = Store(":memory:")
    rows, events, alerts, naive, sim, pipe = run_pipeline_full(cfg, "demo", store=store)
    return store, rows[-1][3]["t"], pipe.alerts.baseline(), pipe


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- digest and chart

def test_digest_is_structured_and_small(cfg, demo_store):
    store, now_t, baseline, _ = demo_store
    d = build_digest(cfg, store, PATIENT, now_t, "overnight", baseline)
    raw = json.dumps(d)
    assert len(raw) < 12_000
    assert "Jordan" not in raw and "Avery" not in raw                 # bed number only
    assert d["data_source"].startswith("SIMULATED")
    assert d["patient"]["risk_factors"]["osa"] == "suspected"         # LLM context only
    assert d["events"]["count_by_type"]["obstructive"] == 3 and d["events"]["count_by_type"]["central"] == 2
    assert any(a["tier"] == 3 for a in d["alerts"])
    assert d["breathing_rate_per_min"]["stats"]["min"] < 7
    # never raw waveforms: no list in the digest holds more than a handful of numbers
    def lists(x):
        if isinstance(x, dict):
            for v in x.values():
                yield from lists(v)
        elif isinstance(x, list):
            yield x
            for v in x:
                yield from lists(v)
    assert all(len(lst) <= cfg.llm.max_alerts_listed for lst in lists(d))


def test_explain_digest_centres_on_the_alert(cfg, demo_store):
    store, now_t, baseline, pipe = demo_store
    alert = next(a for a in pipe.alerts.alerts.values() if a["kind"] == "apnea" and a["tier"] == 3)
    d = build_digest(cfg, store, PATIENT, now_t, "explain", baseline, alert)
    assert d["alert_to_explain"]["reason"] == alert["reason"]


def test_chart_png(cfg, demo_store):
    store, now_t, baseline, _ = demo_store
    png = render_chart(cfg, store, PATIENT, now_t, baseline)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and 10_000 < len(png) < 400_000


def test_system_prompt_rules():
    for phrase in ("Use only the data provided", "No diagnoses", "No medication, dosing",
                   "monitoring and clinician-review", "research prototype", "disagree"):
        assert phrase in SYSTEM_PROMPT


# ---------------------------------------------------------------- summarizer + cache

def test_summaries_and_images(cfg, demo_store):
    store, now_t, baseline, pipe = demo_store
    fake = FakeProvider()
    s = Summarizer(cfg, store, fake)
    over = run(s.summarize("overnight", PATIENT, now_t, baseline))
    assert set(over) == SUMMARY and over["cached"] is False and over["sbar"] is None
    hand = run(s.summarize("handoff", PATIENT, now_t, baseline))
    assert set(hand["sbar"]) == SBAR_KEYS and hand["text"].startswith("Situation:")
    alert = next(iter(pipe.alerts.alerts.values()))
    exp = run(s.summarize("explain", PATIENT, now_t, baseline, alert))
    assert exp["alert_id"] == alert["id"]
    images = {kind: imgs for kind, _, imgs in fake.calls}
    assert len(images["overnight"]) == 1 and len(images["handoff"]) == 1 and images["explain"] == []


def test_cache_fallback_on_failure(cfg):
    store = Store(":memory:")
    ok = Summarizer(cfg, store, FakeProvider())
    first = run(ok.summarize("overnight", PATIENT, time.time()))
    down = Summarizer(cfg, store, FakeProvider(fail=True))
    again = run(down.summarize("overnight", PATIENT, time.time()))
    assert again["cached"] is True and again["text"] == first["text"]
    with pytest.raises(LLMUnavailable):
        run(down.summarize("handoff", PATIENT, time.time()))          # nothing cached for this kind


def test_bad_or_missing_key_falls_back_without_network(cfg, monkeypatch):
    """Acceptance (simulated outage): Gemini cannot be reached -> latest cached summary."""
    import server.llm
    monkeypatch.setattr(server.llm, "load_api_key", lambda *a, **k: (None, "missing"))  # ignore a real .env
    store = Store(":memory:")
    run(Summarizer(cfg, store, FakeProvider()).summarize("overnight", PATIENT, time.time()))
    out = run(Summarizer(cfg, store, GeminiProvider(cfg)).summarize("overnight", PATIENT, time.time()))
    assert out["cached"] is True


def test_timeout_falls_back(cfg):
    class Slow(FakeProvider):
        def summarize(self, kind, digest, images):
            time.sleep(3)
            return "late"
    fast_cfg = with_overrides(cfg, llm={"timeout_s": 0.1, "total_timeout_s": 0.2})
    store = Store(":memory:")
    run(Summarizer(fast_cfg, store, FakeProvider()).summarize("overnight", PATIENT, time.time()))
    out = run(Summarizer(fast_cfg, store, Slow()).summarize("overnight", PATIENT, time.time()))
    assert out["cached"] is True


def test_malformed_handoff_falls_back(cfg):
    class Bad(FakeProvider):
        def summarize(self, kind, digest, images):
            return "not json"
    store = Store(":memory:")
    run(Summarizer(cfg, store, FakeProvider()).summarize("handoff", PATIENT, time.time()))
    assert run(Summarizer(cfg, store, Bad()).summarize("handoff", PATIENT, time.time()))["cached"] is True


# ---------------------------------------------------------------- endpoints

@pytest.fixture
def client(test_cfg):
    cfg = with_overrides(test_cfg, llm={"provider": "fake"}, ward={"backfill_s": 120})
    app = create_app(cfg, source="udp")
    with TestClient(app) as c:
        c.state_ = app.state.bw
        yield c


def test_summary_endpoints(client):
    r = client.post("/api/patients/p6/summary/overnight")
    assert r.status_code == 200 and set(r.json()) == SUMMARY and r.json()["patient_id"] == "p6"
    r = client.post("/api/patients/p6/summary/handoff")
    assert r.status_code == 200 and set(r.json()["sbar"]) == SBAR_KEYS
    r = client.get("/api/patients/p6/summary/chart.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
    assert client.post("/api/patients/p6/summary/explain/999999").status_code == 404
    assert client.post("/api/patients/p404/summary/overnight").status_code == 404
    assert client.post("/api/summary/overnight").json()["patient_id"] == "p4"      # legacy alias


def test_explain_endpoint(client):
    rt = client.state_.ward.runtimes["p1"]
    eng = rt.pipeline.alerts
    eng._raise("rr_low", 2, "Breathing rate 7/min for 65 s (baseline 15/min), signal confidence high", rt.now_t())
    for a in eng.take_changes():
        client.state_.ward.log_alert(rt.id, a)
    aid = eng.active["rr_low"]
    r = client.post(f"/api/patients/p1/summary/explain/{aid}")
    assert r.status_code == 200 and r.json()["alert_id"] == aid
    assert client.post(f"/api/patients/p2/summary/explain/{aid}").status_code == 404   # other patient's alert


def test_503_when_down_and_nothing_cached(client):
    client.state_.summarizer.provider = FakeProvider(fail=True)
    r = client.post("/api/patients/p3/summary/handoff")
    assert r.status_code == 503 and "no cached handoff" in r.json()["detail"]


def test_api_key_environment_wins_over_dotenv(tmp_path, monkeypatch):
    from server.llm import load_api_key
    env = tmp_path / ".env"
    env.write_text("TEST_BW_KEY=from-dotenv\n")
    monkeypatch.delenv("TEST_BW_KEY", raising=False)
    assert load_api_key("TEST_BW_KEY", env) == ("from-dotenv", ".env")
    monkeypatch.setenv("TEST_BW_KEY", "from-env")
    assert load_api_key("TEST_BW_KEY", env) == ("from-env", "environment")
    monkeypatch.delenv("TEST_BW_KEY")
    assert load_api_key("TEST_BW_KEY", tmp_path / "missing.env") == (None, "missing")


def test_key_is_never_logged(cfg, monkeypatch, caplog):
    import server.llm
    secret = "AIzaFAKE-not-a-real-key-123"
    monkeypatch.setattr(server.llm, "load_api_key", lambda *a, **k: (secret, "environment"))

    class Boom:
        class models:
            @staticmethod
            def generate_content(**kw):
                raise RuntimeError(f"request failed for key={secret}")
    prov = GeminiProvider(cfg)
    prov._client = Boom()
    store = Store(":memory:")
    run(Summarizer(cfg, store, FakeProvider()).summarize("overnight", PATIENT, time.time()))
    with caplog.at_level("DEBUG"):
        out = run(Summarizer(cfg, store, prov).summarize("overnight", PATIENT, time.time()))
    assert out["cached"] is True
    assert secret not in caplog.text and "[redacted]" in caplog.text


def test_overloaded_model_falls_through_to_next(cfg, monkeypatch):
    """503 'high demand' on the first model -> the next configured model answers."""
    import server.llm

    class Busy(Exception):
        code = 503

    class Denied(Exception):
        code = 400

    tried = []

    class Client:
        class models:
            @staticmethod
            def generate_content(model, contents, config):
                tried.append(model)
                if model == cfg.llm.model:
                    raise Busy("This model is currently experiencing high demand")
                return type("R", (), {"text": "Chart: flat. Summary: ok. Discrepancies: none"})()

    monkeypatch.setattr(server.llm, "load_api_key", lambda *a, **k: ("k", "environment"))
    prov = GeminiProvider(cfg)
    prov._client = Client()
    out = run(Summarizer(cfg, Store(":memory:"), prov).summarize("overnight", PATIENT, time.time()))
    assert out["cached"] is False and tried == [cfg.llm.model, cfg.llm.fallback_models[0]]
    assert out["model"] == cfg.llm.fallback_models[0]

    def denied(model, contents, config):
        tried.append(model)
        raise Denied("API key not valid")
    tried.clear()
    Client.models.generate_content = staticmethod(denied)
    with pytest.raises(LLMUnavailable):
        run(Summarizer(cfg, Store(":memory:"), prov).summarize("overnight", PATIENT, time.time()))
    assert tried == [cfg.llm.model]          # auth errors do not walk the fallback list
