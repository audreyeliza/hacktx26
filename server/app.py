"""FastAPI app: REST API, live WebSocket, static dashboard.

Run:  python -m server.app            (prints the dashboard URL for phones)
  or  uvicorn server.app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server.config import ROOT, Config, load_config
from server.ingest import CH, Ingest, list_serial_ports, local_ips
from server.signals import compute_vitals
from server.simulator import SCENARIO_NAMES

log = logging.getLogger(__name__)
WEB = ROOT / "web"

# Display name -> raw channel for the live waveforms.
WAVES = {"effort": "pressure", "airflow": "therm", "pulse": "pulse"}


class State:
    """All mutable server state lives here (AGENTS.md section 15)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.ingest = Ingest(cfg)
        self.started = time.time()
        self.step = int(cfg.signals.rate_interval_s * self.ingest.fs)  # samples between updates
        self.vitals: dict[str, Any] | None = None
        self.vitals_seq = 0
        # (vitals_seq, sample time, rr_fused) for the trend chart
        self.trend: deque[tuple[int, float, float | None]] = deque(
            maxlen=int(cfg.live.trend_window_s / cfg.signals.rate_interval_s))
        self._next_idx = self.step
        self._generation = self.ingest.generation

    def process_pending(self, max_windows: int = 10) -> int:
        """Compute vitals for every 5 s boundary of sample time the buffers have passed."""
        ing = self.ingest
        if ing.generation != self._generation:
            self._generation = ing.generation
            self._next_idx = self.step
            self.vitals = None
            self.trend.clear()
        total = ing.buffers.total
        if total - self._next_idx > ing.buffers.capacity:  # fell far behind: skip ahead
            self._next_idx = (total // self.step) * self.step
        done = 0
        while total >= self._next_idx and done < max_windows:
            v = compute_vitals(ing.buffers, self._next_idx, ing.fs, self.cfg)
            v["t"] = ing.sample_time(self._next_idx)
            self.vitals_seq += 1
            self.vitals = v
            self.trend.append((self.vitals_seq, v["t"], v["rr_fused"]))
            self._next_idx += self.step
            done += 1
        return done

    def trend_since(self, seq: int) -> list[tuple[float, float | None]]:
        return [(t, rr) for s, t, rr in self.trend if s > seq]

    def snapshot(self) -> dict[str, Any]:
        return {
            "patient": "Bed 4",
            "ingest": self.ingest.snapshot(),
            "vitals": self.vitals,
            "network": {"ips": local_ips(), "http_port": self.cfg.network.http_port,
                        "udp_port": self.cfg.network.udp_port},
        }

    async def reset(self) -> None:
        self.ingest.reset()
        self.process_pending()


async def processing_loop(state: State) -> None:
    while True:
        try:
            state.process_pending()
        except Exception:  # keep the server alive; one bad window must not stop monitoring
            log.exception("processing failed")
        await asyncio.sleep(0.1)


class SourceRequest(BaseModel):
    source: str
    speed: float | None = None
    loop: bool = False


def _clean(values: np.ndarray, digits: int | None) -> list[float | int | None]:
    """NaN -> null for JSON; ADC channels as ints, accel rounded."""
    if digits is None:
        return [None if math.isnan(v) else int(v) for v in values.tolist()]
    return [None if math.isnan(v) else round(v, digits) for v in values.tolist()]


def wave_message(state: State, start: int, end: int) -> dict[str, Any]:
    """Decimated raw waveforms for samples [start, end), aligned to global sample index."""
    dec = state.cfg.live.wave_decimate
    first = start + (-start) % dec
    n = end - first
    block = state.ingest.buffers.window(end, n) if n > 0 else np.empty((len(CH), 0))
    block = block[:, ::dec]
    msg: dict[str, Any] = {"type": "wave", "start": first, "step": dec, "fs": state.ingest.fs,
                           "end": end, "t_end": state.ingest.sample_time(end)}
    for name, ch in WAVES.items():
        msg[name] = _clean(block[CH[ch]], None)
    return msg


async def live_socket(ws: WebSocket, state: State) -> None:
    cfg = state.cfg.live
    await ws.accept()
    await ws.send_json({"type": "hello", "window_s": cfg.wave_window_s,
                        "trend_window_s": cfg.trend_window_s, "trend": state.trend_since(0),
                        "scenarios": list(SCENARIO_NAMES), "state": state.snapshot()})
    last_vitals = state.vitals_seq
    last_end: int | None = None
    generation = state.ingest.generation
    next_status = 0.0
    window_n = int(cfg.wave_window_s * state.ingest.fs)
    try:
        while True:
            ing = state.ingest
            if ing.generation != generation:
                generation, last_end = ing.generation, None
                await ws.send_json({"type": "reset"})
            if state.vitals_seq != last_vitals:
                if state.vitals_seq < last_vitals:
                    last_vitals = 0
                await ws.send_json({"type": "vitals", "vitals": state.vitals,
                                    "trend": state.trend_since(last_vitals)})
                last_vitals = state.vitals_seq
            end = ing.buffers.total
            start = max(end - window_n if last_end is None else max(last_end, end - window_n), 0)
            if end > start:
                await ws.send_json(wave_message(state, start, end))
                last_end = end
            now = time.monotonic()
            if now >= next_status:
                next_status = now + cfg.status_interval_s
                await ws.send_json({"type": "status", **state.snapshot()})
            await asyncio.sleep(1.0 / cfg.push_hz)
    except (WebSocketDisconnect, RuntimeError):
        pass


def create_app(cfg: Config | None = None, source: str | None = None) -> FastAPI:
    cfg = cfg or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not logging.getLogger().handlers:
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        state = State(cfg)
        app.state.bw = state
        try:
            await state.ingest.set_source(source or cfg.ingest.default_source)
        except ValueError as exc:
            log.error("%s; starting with no source", exc)
        for ip in local_ips():
            log.info("Dashboard: http://%s:%d   (Pico SERVER_IP=%s, UDP port %d)",
                     ip, cfg.network.http_port, ip, cfg.network.udp_port)
        processor = asyncio.create_task(processing_loop(state))
        yield
        processor.cancel()
        await state.ingest.stop()

    app = FastAPI(title="BreathWatch", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.middleware("http")
    async def no_cache_static(request: Request, call_next):
        # Browsers revalidate (cheap 304s) so dashboard edits show up on a plain reload.
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    def st() -> State:
        return app.state.bw

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB / "index.html")

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        return st().snapshot()

    @app.post("/api/source")
    async def post_source(req: SourceRequest) -> dict[str, Any]:
        try:
            await st().ingest.set_source(req.source, req.speed, req.loop)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return st().snapshot()

    @app.get("/api/serial/ports")
    async def serial_ports() -> list[dict[str, Any]]:
        return list_serial_ports(cfg.ingest.serial_pico_vid)

    @app.post("/api/demo/reset")
    async def demo_reset() -> dict[str, Any]:
        await st().reset()
        return st().snapshot()

    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket) -> None:
        await live_socket(ws, st())

    return app


app = create_app()


def main() -> None:
    import uvicorn

    cfg = load_config()
    uvicorn.run(app, host=cfg.network.http_host, port=cfg.network.http_port, log_level="info")


if __name__ == "__main__":
    main()
