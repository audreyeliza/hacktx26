"""FastAPI app: the clinical app shell API (AGENTS.md §19), WebSockets, pages.

Run:  python -m server.app            (prints the dashboard URL for phones)
  or  uvicorn server.app:app --host 0.0.0.0 --port 8000

The single-patient endpoints of §11 (/api/state, /api/events, /api/alerts, /ws/live, …) remain
as deprecated aliases for the patient wearing the live device (§19.8).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server.auth import ADMIN, CAN_EDIT_PATIENTS, Sessions
from server.config import ROOT, Config, load_config
from server.ingest import CH, Ingest, list_serial_ports, local_ips
from server.llm import LLMUnavailable, Summarizer, render_chart
from server.patients import PatientCreate, PatientRuntime, PatientUpdate, Ward, WardError
from server.public_demo import RateLimiter, accept_wearable, inject_banner, is_enabled
from server.quality_model import QualityModel
from server.record import Recorder
from server.simulator import SCENARIO_NAMES
from server.store import Store

log = logging.getLogger(__name__)
WEB = ROOT / "web"

# Display name -> raw channel for the live waveforms.
WAVES = {"effort": "pressure", "airflow": "therm", "pulse": "pulse"}
PAGES = {"/login": "login.html", "/patients/new": "patient-new.html",
         "/admin/device": "admin.html"}


class State:
    """All mutable server state lives here (AGENTS.md section 15)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.public = is_enabled(cfg)    # PUBLIC_DEMO (AGENTS.md §20)
        self.store = Store(cfg.store.path)
        self.ingest = Ingest(cfg)
        self.ward = Ward(cfg, self.store, self.ingest, public=self.public)
        self.quality_model = QualityModel(cfg)          # v1 if a usable trained model exists, else rules
        self.ward.quality_model = self.quality_model
        self.limiter = RateLimiter()
        self.last_reset_t = 0.0
        self.sessions = Sessions(cfg.auth)
        self.recorder = Recorder(cfg)
        self.ingest.on_packet = self.recorder.on_packet
        self.recorder.source_fn = lambda: self.ingest.source_spec
        self.summarizer = Summarizer(cfg, self.store)
        self.started = time.time()

    def network(self) -> dict[str, Any]:
        return {"ips": local_ips(), "http_port": self.cfg.network.http_port, "udp_port": self.cfg.network.udp_port}

    def admin_device(self) -> dict[str, Any]:
        ing = self.ingest
        live = self.ward.live_runtime()
        return {
            "source": ing.source_spec,
            "source_status": ing.source.status if ing.source else "stopped",
            "link_ok": ing.link_ok(),
            "stats": {"packets": ing.packets, "invalid": ing.invalid, "unassigned_packets": ing.unassigned_packets,
                      "nonjson_lines": ing.nonjson_lines, "last_arrival": ing.last_arrival,
                      "last_device": ing.last_device,
                      "pico_addr": list(ing.pico_addr) if ing.pico_addr else None,
                      **({k: v for k, v in asdict(live.stream.stats).items()
                          if k in ("gaps", "missing_packets", "dropped_late", "restarts")} if live else {})},
            "assigned_patient_id": live.id if live else None,
            "unassigned_packets": ing.unassigned_packets,
            "network": self.network(),
            "recording": self.recorder.status(),
            "quality_model": self.quality_model.status,
        }

    def legacy_snapshot(self, rt: PatientRuntime) -> dict[str, Any]:
        """The pre-§19 single-patient /api/state shape, for the live patient."""
        ing = self.ingest
        tier, reason = rt.tier()
        return {
            "patient": f"Bed {rt.patient['bed']}",
            "ingest": {"source": ing.source_spec, "source_status": ing.source.status if ing.source else "stopped",
                       "link_ok": rt.stream.link_ok(), "samples": rt.stream.buffers.total,
                       "sample_time": rt.stream.sample_time(rt.stream.buffers.total),
                       "stats": asdict(rt.stream.stats)},
            "vitals": rt.vitals, "recent_events": rt.recent_events(10), "ongoing_event": rt.pipeline.ongoing,
            "tier": tier, "tier_reason": reason, "baseline": rt.pipeline.alerts.baseline(),
            "active_alerts": rt.pipeline.alerts.active_alerts(), "monitors": rt.pipeline.monitors(),
            "bedside": rt.bedside_command(), "network": self.network(),
        }


async def processing_loop(state: State) -> None:
    while True:
        state.ward.process_all()
        await asyncio.sleep(0.1)


async def bedside_loop(state: State) -> None:
    """Keep the Pico's LED/buzzer in sync with the live patient (it blinks blue after 10 s)."""
    while True:
        try:
            rt = state.ward.live_runtime()
            if rt is not None:
                state.ingest.send_command(rt.bedside_command())
        except Exception:
            log.exception("bedside command failed")
        await asyncio.sleep(state.cfg.bedside.command_interval_s)


# --------------------------------------------------------------------------- request bodies

class LoginRequest(BaseModel):
    username: str
    password: str


class DemoLoginRequest(BaseModel):
    role: str


class AckRequest(BaseModel):
    note: str | None = None


class SourceRequest(BaseModel):
    source: str
    speed: float | None = None
    loop: bool = False


class RecordRequest(BaseModel):
    label: str
    session: str | None = None   # tools/record_session.py: one folder per guided session
    segment: str | None = None   # e.g. "03-slow_breathing" → file name prefix


class ResetRequest(BaseModel):
    reseed_ward: bool = False


# --------------------------------------------------------------------------- sockets

def _clean(values: np.ndarray) -> list[int | None]:
    """NaN -> null for JSON; ADC counts as ints."""
    return [None if math.isnan(v) else int(v) for v in values.tolist()]


def wave_message(cfg: Config, rt: PatientRuntime, start: int, end: int) -> dict[str, Any]:
    """Decimated raw waveforms for samples [start, end), aligned to the global sample index."""
    dec = cfg.live.wave_decimate
    first = start + (-start) % dec
    n = end - first
    block = rt.stream.buffers.window(end, n) if n > 0 else np.empty((len(CH), 0))
    block = block[:, ::dec]
    msg: dict[str, Any] = {"type": "wave", "start": first, "step": dec, "fs": rt.fs, "end": end,
                           "t_end": rt.stream.sample_time(end)}
    for name, ch in WAVES.items():
        msg[name] = _clean(block[CH[ch]])
    return msg


async def patient_socket(ws: WebSocket, state: State, patient_id: str, legacy: bool = False) -> None:
    """WS /ws/patients/{id} (§19.6), or the legacy /ws/live shape for the live patient."""
    cfg = state.cfg.live
    ward = state.ward
    rt = ward.runtimes.get(patient_id)
    await ws.accept()
    if rt is None:
        await ws.close(code=4404)
        return

    def hello() -> dict[str, Any]:
        base = {"type": "hello", "window_s": cfg.wave_window_s, "trend_window_s": cfg.trend_window_s,
                "trend": rt.trend_since(0)}
        if legacy:
            return {**base, "scenarios": list(SCENARIO_NAMES), "state": state.legacy_snapshot(rt)}
        return {**base, "patient": rt.patient, "state": rt.state()}

    def status() -> dict[str, Any]:
        if legacy:
            return {"type": "status", **state.legacy_snapshot(rt)}
        tier, _ = rt.tier()
        return {"type": "status", "device": rt.device_status(), "tier": tier, "monitors": rt.pipeline.monitors()}

    await ws.send_json(hello())
    last_vitals, last_event, last_alert = rt.vitals_seq, rt.event_seq, ward.alert_seq
    generation, last_end, next_status = rt.generation, None, 0.0
    window_n = int(cfg.wave_window_s * rt.fs)
    try:
        while True:
            current = ward.runtimes.get(patient_id)
            if current is None:                       # discharged
                if not legacy:
                    await ws.send_json({"type": "discharged", "patient": ward.get(patient_id)})
                await ws.close(code=1000)
                return
            if current is not rt or current.generation != generation:
                rt, generation = current, current.generation
                last_vitals = last_event = 0
                last_end = None
                await ws.send_json({"type": "reset"})
            if rt.vitals_seq != last_vitals:
                await ws.send_json({"type": "vitals", "vitals": rt.vitals, "trend": rt.trend_since(last_vitals)})
                last_vitals = rt.vitals_seq
            if rt.event_seq != last_event:
                for e in rt.events_since(last_event):
                    await ws.send_json({"type": "event", "event": e})
                last_event = rt.event_seq
            if ward.alert_seq != last_alert:
                for _, a in ward.alerts_since(last_alert, patient_id):
                    await ws.send_json({"type": "alert", "alert": a})
                last_alert = ward.alert_seq
            end = rt.stream.buffers.total
            start = max(end - window_n if last_end is None else max(last_end, end - window_n), 0)
            if end > start:
                await ws.send_json(wave_message(state.cfg, rt, start, end))
                last_end = end
            now = time.monotonic()
            if now >= next_status:
                next_status = now + cfg.status_interval_s
                await ws.send_json(status())
            await asyncio.sleep(1.0 / cfg.push_hz)
    except (WebSocketDisconnect, RuntimeError):
        pass


async def ward_socket(ws: WebSocket, state: State) -> None:
    """WS /ws/ward (§19.6): ward summaries (at most every ward_push_s) + immediate alert pushes."""
    ward = state.ward
    await ws.accept()

    def listing() -> tuple[dict[str, Any], str]:
        data = ward.listing()
        key = json.dumps(data["patients"], sort_keys=True, default=str)
        return data, key

    data, last_key = listing()
    await ws.send_json({"type": "ward", **data})
    last_alert, last_push = ward.alert_seq, time.monotonic()
    try:
        while True:
            if ward.alert_seq != last_alert:
                for pid, a in ward.alerts_since(last_alert):
                    await ws.send_json({"type": "alert", "patient_id": pid, "alert": a})
                last_alert = ward.alert_seq
            if time.monotonic() - last_push >= state.cfg.ward.ward_push_s:
                data, key = listing()
                if key != last_key:
                    await ws.send_json({"type": "ward", **data})
                    last_key = key
                last_push = time.monotonic()
            await asyncio.sleep(0.25)
    except (WebSocketDisconnect, RuntimeError):
        pass


# --------------------------------------------------------------------------- app

def create_app(cfg: Config | None = None, source: str | None = None) -> FastAPI:
    cfg = cfg or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not logging.getLogger().handlers:
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        state = State(cfg)
        app.state.bw = state
        await state.ward.start()
        wearable = state.public and accept_wearable()
        if wearable:
            log.warning("PUBLIC DEMO mode: wearable UDP on port %s, admin still locked",
                        cfg.network.udp_port)
            try:
                await state.ingest.set_source("udp")
            except ValueError as exc:
                log.error("%s; starting with no source", exc)
        elif state.public:
            log.warning("PUBLIC DEMO mode: simulated beds only, no UDP/serial listener, admin locked")
        else:
            try:
                await state.ingest.set_source(source or cfg.ingest.default_source)
            except ValueError as exc:
                log.error("%s; starting with no source", exc)
            for ip in local_ips():
                log.info("Dashboard: http://%s:%d   (Pico SERVER_IP=%s, UDP port %d)",
                         ip, cfg.network.http_port, ip, cfg.network.udp_port)
        tasks = [asyncio.create_task(processing_loop(state))]
        if not state.public or wearable:
            tasks.append(asyncio.create_task(bedside_loop(state)))
        yield
        for task in tasks:
            task.cancel()
        state.recorder.stop()
        await state.ingest.stop()
        await state.ward.stop()
        state.store.close()

    app = FastAPI(title="Respiro", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.middleware("http")
    async def no_cache_pages(request: Request, call_next):
        # Browsers revalidate (cheap 304s) so frontend edits show up on a plain reload.
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(WardError)
    async def ward_error(request: Request, exc: WardError) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    def st() -> State:
        return app.state.bw

    # ---- auth dependencies (§19.4)
    def signed_in(request: Request) -> dict[str, str]:
        return st().sessions.require(request)

    def can_edit(request: Request) -> dict[str, str]:
        return st().sessions.require(request, CAN_EDIT_PATIENTS)

    def admin_only(request: Request) -> dict[str, str]:
        return st().sessions.require(request, ADMIN)

    def not_public(what: str) -> None:
        if st().public:
            raise HTTPException(403, f"{what} is disabled in the public demo")

    # ---- pages
    def page(name: str) -> Response:
        path = WEB / name
        if not path.exists():
            raise HTTPException(404, f"page {name} not built yet")
        if st().public:   # point 6: the banner is on every page, whatever the frontend does
            return HTMLResponse(inject_banner(path.read_text(), cfg.public_demo.banner))
        return FileResponse(path)

    for route, name in PAGES.items():
        app.add_api_route(route, (lambda n=name: page(n)), methods=["GET"], include_in_schema=False)

    @app.get("/", include_in_schema=False)
    async def home(request: Request) -> Response:
        """Welcome when signed out; the ward once a session exists (§19.5).

        auth.enabled false counts as signed in, so that mode still gets the ward.
        """
        if st().sessions.user_for(request.cookies) is None:
            return page("welcome.html")
        return page("index.html")

    @app.get("/patients/{patient_id}", include_in_schema=False)
    async def patient_page(patient_id: str) -> Response:
        return page("patient.html")

    # ---- auth
    def _set_cookie(response: Response, user: dict[str, str]) -> None:
        token = st().sessions.create(user)
        response.set_cookie(cfg.auth.cookie_name, token, httponly=True, samesite="lax", path="/",
                            max_age=int(cfg.auth.session_ttl_h * 3600), secure=st().public)  # public: HTTPS

    @app.post("/api/auth/login")
    async def login(req: LoginRequest, response: Response) -> dict[str, Any]:
        if st().public:
            raise HTTPException(403, "password login is disabled in the public demo; use a demo sign-in button")
        user = st().sessions.check_password(req.username, req.password)
        if user is None:
            raise HTTPException(401, "wrong username or password")
        _set_cookie(response, user)
        return {"user": user}

    @app.post("/api/auth/demo")
    async def demo_login(req: DemoLoginRequest, response: Response) -> dict[str, Any]:
        user = st().sessions.account_for_role(req.role)
        if user is None:
            raise HTTPException(400, f"no demo account with role {req.role!r}")
        _set_cookie(response, user)
        return {"user": user}

    @app.post("/api/auth/logout", status_code=204)
    async def logout(request: Request) -> Response:
        st().sessions.delete(request.cookies.get(cfg.auth.cookie_name))
        response = Response(status_code=204)
        response.delete_cookie(cfg.auth.cookie_name, path="/")
        return response

    @app.get("/api/auth/me")
    async def me(user: dict = Depends(signed_in)) -> dict[str, Any]:
        return {"user": user}

    @app.get("/api/public-config")
    async def public_config() -> dict[str, Any]:
        """For the frontend (no sign-in needed): what this deployment allows (§20)."""
        public = st().public
        return {"public_demo": public, "banner": cfg.public_demo.banner if public else None,
                "password_login": not public, "live_device": (not public) or accept_wearable()}

    @app.get("/api/auth/demo-accounts")
    async def demo_accounts() -> list[dict[str, str]]:
        return st().sessions.demo_accounts()

    # ---- ward and patients
    @app.get("/api/patients")
    async def list_patients(status: str = "admitted", user: dict = Depends(signed_in)) -> dict[str, Any]:
        return st().ward.listing(status)

    @app.post("/api/patients", status_code=201)
    async def create_patient(body: PatientCreate, user: dict = Depends(can_edit)) -> dict[str, Any]:
        return await st().ward.create(body)

    @app.get("/api/patients/{patient_id}")
    async def get_patient(patient_id: str, user: dict = Depends(signed_in)) -> dict[str, Any]:
        return st().ward.get(patient_id)

    @app.patch("/api/patients/{patient_id}")
    async def update_patient(patient_id: str, body: PatientUpdate, user: dict = Depends(can_edit)) -> dict[str, Any]:
        return await st().ward.update(patient_id, body)

    @app.post("/api/patients/{patient_id}/discharge")
    async def discharge_patient(patient_id: str, user: dict = Depends(can_edit)) -> dict[str, Any]:
        return await st().ward.discharge(patient_id)

    @app.get("/api/patients/{patient_id}/state")
    async def patient_state(patient_id: str, user: dict = Depends(signed_in)) -> dict[str, Any]:
        return st().ward.state_for(patient_id)

    @app.get("/api/patients/{patient_id}/vitals")
    async def patient_vitals(patient_id: str, since_t: float | None = None, limit: int = 720,
                             user: dict = Depends(signed_in)) -> dict[str, Any]:
        st().ward.get(patient_id)
        return {"vitals": st().store.vitals(patient_id, since_t, min(limit, 5000))}

    @app.get("/api/patients/{patient_id}/events")
    async def patient_events(patient_id: str, since_t: float | None = None, limit: int = 100,
                             user: dict = Depends(signed_in)) -> dict[str, Any]:
        st().ward.get(patient_id)
        return {"events": st().store.events(patient_id, since_t, min(limit, 1000))}

    @app.get("/api/patients/{patient_id}/alerts")
    async def patient_alerts(patient_id: str, status: str = "all", since_t: float | None = None,
                             limit: int = 100, user: dict = Depends(signed_in)) -> dict[str, Any]:
        st().ward.get(patient_id)
        return {"alerts": st().store.alerts(patient_id, status, since_t, min(limit, 1000))}

    def _ack(patient_id: str, alert_id: int, user: dict | None, note: str | None) -> dict[str, Any]:
        ward = st().ward
        ward.get(patient_id)
        rt = ward.runtimes.get(patient_id)
        if rt is not None and alert_id in rt.pipeline.alerts.alerts:
            return rt.acknowledge(alert_id, user, note)  # type: ignore[return-value]
        stored = st().store.alert(alert_id)
        if stored is None or stored["patient_id"] != patient_id:
            raise HTTPException(404, f"no alert {alert_id} for patient {patient_id}")
        if stored["status"] in ("active", "awaiting_ack"):
            # Left open by an earlier run (server restart or pipeline reset): the engine no longer
            # tracks it, so close it here or it stays listed as unacknowledged forever.
            now = time.time()
            stored.update(status="acknowledged", acknowledged_by=user, acknowledged_t=now,
                          ack_note=note, updated_t=now)
            st().store.save_alert(stored)
            ward.log_alert(patient_id, stored)
        return stored

    @app.post("/api/patients/{patient_id}/alerts/{alert_id}/ack")
    async def ack_alert(patient_id: str, alert_id: int, req: AckRequest | None = None,
                        user: dict = Depends(signed_in)) -> dict[str, Any]:
        return _ack(patient_id, alert_id, user, req.note if req else None)

    # ---- LLM summaries (§10, §19.5) — explanations only; they never change alerts
    def _summary_context(patient_id: str) -> tuple[dict[str, Any], float, dict[str, Any] | None]:
        ward = st().ward
        patient = ward.get(patient_id)
        rt = ward.runtimes.get(patient_id)
        now_t = rt.now_t() if rt else (patient.get("discharged_t") or time.time())
        baseline = rt.pipeline.alerts.baseline() if rt else None
        return patient, now_t, baseline

    def _summary_allowed(request: Request) -> bool:
        """Point 5: per-session and global Gemini limits in the public demo."""
        if not st().public:
            return True
        pd = cfg.public_demo
        session = request.cookies.get(cfg.auth.cookie_name) or (request.client.host if request.client else "?")
        return st().limiter.check([(("summary", session), pd.summary_per_session_per_h, 3600.0),
                                   (("summary", "*"), pd.summary_global_per_h, 3600.0)])

    async def _summary(patient_id: str, kind: str, alert_id: int | None = None,
                       request: Request | None = None) -> dict[str, Any]:
        patient, now_t, baseline = _summary_context(patient_id)
        alert = None
        if kind == "explain":
            rt = st().ward.runtimes.get(patient_id)
            alert = (rt.pipeline.alerts.alerts.get(alert_id) if rt else None) or st().store.alert(alert_id)
            if alert is None or alert["patient_id"] != patient_id:
                raise HTTPException(404, f"no alert {alert_id} for patient {patient_id}")
        if request is not None and not _summary_allowed(request):
            cached = st().store.cached_summary(patient_id, kind, str(alert_id) if kind == "explain" else None)
            if cached is None:
                raise HTTPException(429, "summary limit reached in the public demo and nothing cached yet; try later")
            return {**cached, "cached": True}
        try:
            return await st().summarizer.summarize(kind, patient, now_t, baseline, alert)
        except LLMUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/patients/{patient_id}/summary/explain/{alert_id}")
    async def summary_explain(patient_id: str, alert_id: int, request: Request,
                              user: dict = Depends(signed_in)) -> dict[str, Any]:
        return await _summary(patient_id, "explain", alert_id, request)

    @app.post("/api/patients/{patient_id}/summary/overnight")
    async def summary_overnight(patient_id: str, request: Request, user: dict = Depends(signed_in)) -> dict[str, Any]:
        return await _summary(patient_id, "overnight", request=request)

    @app.post("/api/patients/{patient_id}/summary/handoff")
    async def summary_handoff(patient_id: str, request: Request, user: dict = Depends(signed_in)) -> dict[str, Any]:
        return await _summary(patient_id, "handoff", request=request)

    @app.get("/api/patients/{patient_id}/summary/chart.png")
    async def summary_chart(patient_id: str, user: dict = Depends(signed_in)) -> Response:
        patient, now_t, baseline = _summary_context(patient_id)
        png = await asyncio.to_thread(render_chart, cfg, st().store, patient, now_t, baseline)
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-cache"})

    # ---- "calibrate supine" (M10)
    @app.post("/api/patients/{patient_id}/calibrate/supine")
    async def calibrate_patient(patient_id: str, user: dict = Depends(can_edit)) -> dict[str, Any]:
        return st().ward.runtime(patient_id).calibrate_supine()

    @app.delete("/api/patients/{patient_id}/calibration")
    async def clear_calibration(patient_id: str, user: dict = Depends(can_edit)) -> dict[str, Any]:
        return st().ward.runtime(patient_id).clear_calibration()

    # ---- admin: device & data
    @app.get("/api/admin/device")
    async def admin_device(user: dict = Depends(admin_only)) -> dict[str, Any]:
        return st().admin_device()

    @app.post("/api/admin/quality-model/reload")
    async def reload_quality_model(user: dict = Depends(admin_only)) -> dict[str, Any]:
        """Pick up a newly trained data/quality_model.joblib without restarting."""
        return {"quality_model": await asyncio.to_thread(st().quality_model.reload)}

    @app.post("/api/source")
    async def post_source(req: SourceRequest, user: dict = Depends(admin_only)) -> dict[str, Any]:
        not_public("source switching")
        try:
            await st().ingest.set_source(req.source, req.speed, req.loop)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return st().admin_device()

    @app.get("/api/serial/ports")
    async def serial_ports(user: dict = Depends(admin_only)) -> list[dict[str, Any]]:
        not_public("serial ports")
        return list_serial_ports(cfg.ingest.serial_pico_vid)

    @app.post("/api/record/start")
    async def record_start(req: RecordRequest, user: dict = Depends(admin_only)) -> dict[str, Any]:
        not_public("recording")
        try:
            return {"recording": st().recorder.start(req.label, req.session, req.segment)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/record/stop")
    async def record_stop(user: dict = Depends(admin_only)) -> dict[str, Any]:
        return {"recording": None, "file": st().recorder.stop()}

    @app.post("/api/demo/reset")
    async def demo_reset(req: ResetRequest | None = None, user: dict = Depends(admin_only)) -> Any:
        if st().public:
            wait = st().last_reset_t + cfg.public_demo.reset_cooldown_s - time.time()
            if wait > 0:
                return JSONResponse({"detail": f"demo reset is rate-limited in the public demo; try again in "
                                               f"{wait:.0f} s"}, status_code=429,
                                    headers={"Retry-After": str(int(wait) + 1)})
            st().last_reset_t = time.time()
        await st().ward.reset(reseed=bool(req and req.reseed_ward))
        return st().ward.listing()

    # ---- sockets
    @app.websocket("/ws/ward")
    async def ws_ward(ws: WebSocket) -> None:
        if await st().sessions.require_ws(ws) is not None:
            await ward_socket(ws, st())

    @app.websocket("/ws/patients/{patient_id}")
    async def ws_patient(ws: WebSocket, patient_id: str) -> None:
        if await st().sessions.require_ws(ws) is not None:
            await patient_socket(ws, st(), patient_id)

    # ---- deprecated single-patient aliases (§19.8): the patient wearing the live device
    def live() -> PatientRuntime:
        rt = st().ward.live_runtime()
        if rt is None:
            raise HTTPException(404, f"no admitted patient has device {cfg.ward.live_device_id}")
        return rt

    @app.post("/api/calibrate/supine")
    async def calibrate_live(user: dict = Depends(can_edit)) -> dict[str, Any]:
        """The path in AGENTS.md M10: the patient wearing the live device."""
        return live().calibrate_supine()

    @app.get("/api/state")
    async def legacy_state(user: dict = Depends(signed_in)) -> dict[str, Any]:
        return st().legacy_snapshot(live())

    @app.get("/api/events")
    async def legacy_events(limit: int = 100, user: dict = Depends(signed_in)) -> dict[str, Any]:
        return {"events": st().store.events(live().id, limit=limit)}

    @app.get("/api/alerts")
    async def legacy_alerts(status: str | None = None, limit: int = 100, user: dict = Depends(signed_in)) -> dict[str, Any]:
        return {"alerts": st().store.alerts(live().id, status=status, limit=limit)}

    @app.post("/api/alerts/{alert_id}/ack")
    async def legacy_ack(alert_id: int, req: AckRequest | None = None, user: dict = Depends(signed_in)) -> dict[str, Any]:
        return _ack(live().id, alert_id, user, req.note if req else None)

    @app.post("/api/summary/explain/{alert_id}")
    async def legacy_explain(alert_id: int, user: dict = Depends(signed_in)) -> dict[str, Any]:
        return await _summary(live().id, "explain", alert_id)

    @app.post("/api/summary/overnight")
    async def legacy_overnight(user: dict = Depends(signed_in)) -> dict[str, Any]:
        return await _summary(live().id, "overnight")

    @app.post("/api/summary/handoff")
    async def legacy_handoff(user: dict = Depends(signed_in)) -> dict[str, Any]:
        return await _summary(live().id, "handoff")

    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket) -> None:
        if await st().sessions.require_ws(ws) is None:
            return
        rt = st().ward.live_runtime()
        if rt is None:
            await ws.accept()
            await ws.close(code=4404)
            return
        await patient_socket(ws, st(), rt.id, legacy=True)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    cfg = load_config()
    if is_enabled(cfg):   # point 7: only the local reverse proxy can reach us; trust its headers
        uvicorn.run(app, host=cfg.public_demo.http_host, port=cfg.network.http_port, log_level="info",
                    proxy_headers=True, forwarded_allow_ips=cfg.public_demo.proxy_ips)
    else:
        uvicorn.run(app, host=cfg.network.http_host, port=cfg.network.http_port, log_level="info")


if __name__ == "__main__":
    main()
