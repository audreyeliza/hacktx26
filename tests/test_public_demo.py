"""PUBLIC_DEMO mode (AGENTS.md §20): the seven approved rules."""

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.llm import FakeProvider
from server.public_demo import RateLimiter, inject_banner
from tests.conftest import with_overrides

RF = {"opioids": True, "supplemental_o2": False, "osa": "none", "age_over_65": False}


@pytest.fixture
def public(auth_cfg, monkeypatch):
    monkeypatch.setenv("PUBLIC_DEMO", "1")
    cfg = with_overrides(auth_cfg, llm={"provider": "fake"},
                         public_demo={"summary_per_session_per_h": 2, "summary_global_per_h": 3,
                                      "max_patients": 7, "reset_cooldown_s": 120})
    app = create_app(cfg)
    with TestClient(app, base_url="https://testserver") as c:   # the session cookie is Secure (HTTPS)
        c.st = app.state.bw
        yield c


def sign_in(c, role="nurse"):
    assert c.post("/api/auth/demo", json={"role": role}).status_code == 200


def test_1_enabled_by_env(public):
    assert public.st.public is True
    assert public.get("/api/public-config").json() == {
        "public_demo": True, "banner": "Public demo — simulated data, research prototype",
        "password_login": False, "live_device": False}


def test_2_simulated_beds_only(public):
    sign_in(public, "admin")
    assert public.st.ingest.source is None                      # no UDP / serial listener
    rows = public.get("/api/patients").json()["patients"]
    assert all(r["device_kind"] == "simulator" for r in rows)
    bed4 = next(r for r in rows if r["bed"] == "4")
    assert bed4["scenario"] == "demo"
    r = public.post("/api/patients", json={"name": "x", "bed": "9", "risk_factors": RF,
                                           "device": {"kind": "live", "device_id": "patch-01"}})
    assert r.status_code == 400 and "public demo" in r.json()["detail"]
    r = public.patch("/api/patients/p1", json={"device": {"kind": "live", "device_id": "patch-01"}})
    assert r.status_code == 400
    assert public.get("/api/state").status_code == 404           # no live patient for the legacy alias


def test_ward_size_is_capped(public):
    sign_in(public)
    body = {"name": "x", "bed": "9", "risk_factors": RF, "device": {"kind": "simulator", "scenario": "normal"}}
    assert public.post("/api/patients", json=body).status_code == 201     # 7th patient
    r = public.post("/api/patients", json={**body, "bed": "10"})
    assert r.status_code == 429 and "full" in r.json()["detail"]


def test_3_admin_locked_and_reset_rate_limited(public):
    sign_in(public, "admin")
    assert public.post("/api/source", json={"source": "udp"}).status_code == 403
    assert public.get("/api/serial/ports").status_code == 403
    assert public.post("/api/record/start", json={"label": "clean"}).status_code == 403
    assert public.post("/api/demo/reset").status_code == 200
    r = public.post("/api/demo/reset")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0


def test_4_password_login_off_demo_buttons_on(public):
    r = public.post("/api/auth/login", json={"username": "nurse", "password": "demo"})
    assert r.status_code == 403 and "demo sign-in" in r.json()["detail"]
    r = public.post("/api/auth/demo", json={"role": "physician"})
    assert r.status_code == 200
    assert "secure" in r.headers["set-cookie"].lower()           # HTTPS behind the proxy
    plain = TestClient(public.app, base_url="http://testserver")
    plain.cookies = public.cookies
    assert plain.get("/api/auth/me").status_code == 401           # never sent over plain HTTP


def test_5_gemini_limits_fall_back_to_cache(public):
    sign_in(public)
    fake = FakeProvider()
    public.st.summarizer.provider = fake
    assert public.post("/api/patients/p1/summary/overnight").json()["cached"] is False
    assert public.post("/api/patients/p1/summary/overnight").json()["cached"] is False
    third = public.post("/api/patients/p1/summary/overnight")       # per-session limit (2/h) reached
    assert third.status_code == 200 and third.json()["cached"] is True and len(fake.calls) == 2
    r = public.post("/api/patients/p2/summary/handoff")             # nothing cached for this one
    assert r.status_code == 429
    public.cookies.clear()
    sign_in(public, "physician")                                    # new session: 1 left globally (3/h)
    assert public.post("/api/patients/p2/summary/overnight").json()["cached"] is False
    assert public.post("/api/patients/p2/summary/overnight").json()["cached"] is True
    assert len(fake.calls) == 3


def test_welcome_then_ward_in_public_demo(public):
    welcome = public.get("/")
    assert welcome.status_code == 200
    assert "simulated six-bed ward" in welcome.text
    assert "public-demo-banner" in welcome.text
    sign_in(public, "physician")
    ward = public.get("/")
    assert 'id="board"' in ward.text and "simulated six-bed ward" not in ward.text


def test_6_banner_on_every_page(public):
    for path in ("/", "/login", "/patients/p1", "/patients/new", "/admin/device"):
        r = public.get(path)
        assert r.status_code == 200 and "public-demo-banner" in r.text, path
        assert r.text.index("public-demo-banner") > r.text.index("<body")


def test_not_public_by_default(test_cfg, monkeypatch):
    monkeypatch.delenv("PUBLIC_DEMO", raising=False)
    with TestClient(create_app(test_cfg, source="udp")) as c:
        assert c.get("/api/public-config").json()["public_demo"] is False
        home = c.get("/").text
        assert "public-demo-banner" not in home
        assert 'id="board"' in home and "simulated six-bed ward" not in home


def test_7_binds_localhost_behind_proxy(cfg):
    assert cfg.public_demo.http_host == "127.0.0.1" and cfg.public_demo.proxy_ips == "127.0.0.1"
    import inspect
    import server.app
    src = inspect.getsource(server.app.main)
    assert "proxy_headers=True" in src and "cfg.public_demo.http_host" in src


def test_rate_limiter_window():
    rl = RateLimiter()
    assert rl.check([("a", 2, 10.0)], now=0) and rl.check([("a", 2, 10.0)], now=1)
    assert not rl.check([("a", 2, 10.0)], now=2)
    assert rl.check([("a", 2, 10.0)], now=10.5)                    # oldest hit left the window
    assert not rl.check([("b", 5, 10.0), ("a", 2, 10.0)], now=10.6) and rl.check([("b", 5, 10.0)], now=10.6)


def test_banner_injection_escapes():
    out = inject_banner("<html><body class='x'><p>hi</p></body></html>", "<b>demo</b>")
    assert "&lt;b&gt;demo&lt;/b&gt;" in out and out.index("public-demo-banner") > out.index("<body")
