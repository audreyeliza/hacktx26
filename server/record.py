"""Record mode (AGENTS.md §5, §12): raw live samples + a label to CSV, for training the
signal-quality model (milestone 9). Raw waveforms are only ever stored here."""

from __future__ import annotations

import csv
import re
import time
from pathlib import Path
from typing import IO, Any

from server.config import ROOT, Config
from server.ingest import CHANNELS

COLUMNS = ("device", "seq", "k", "t0_ms", *CHANNELS, "label")
NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # session / segment names: no path tricks


def _shown(path: Path) -> str:
    """Repo-relative path when inside the repo, else absolute."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


class Recorder:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.dir = ROOT / cfg.record.dir
        self.label: str | None = None
        self.started_t: float | None = None
        self.samples = 0
        self.path: Path | None = None
        self.session: str | None = None
        self._file: IO[str] | None = None
        self._writer: Any = None

    @property
    def active(self) -> bool:
        return self._file is not None

    def status(self) -> dict[str, Any] | None:
        if not self.active:
            return None
        return {"label": self.label, "session": self.session, "started_t": self.started_t,
                "samples": self.samples, "file": _shown(self.path) if self.path else None}

    def start(self, label: str, session: str | None = None, segment: str | None = None) -> dict[str, Any]:
        """Start a labelled recording. With `session`, files go to <dir>/<session>/ (one folder per
        guided session, tools/record_session.py) and are named <segment>_<label>.csv."""
        if label not in self.cfg.record.labels:
            raise ValueError(f"label must be one of {self.cfg.record.labels}")
        for name in (session, segment):
            if name is not None and not NAME.match(name):
                raise ValueError("session / segment may only use letters, digits, '-' and '_' (max 64)")
        self.stop()
        folder = self.dir / session if session else self.dir
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = folder / (f"{segment}_{label}.csv" if segment else f"{stamp}_{label}.csv")
        self.session = session
        self._file = self.path.open("w", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(COLUMNS)
        self.label, self.started_t, self.samples = label, time.time(), 0
        return self.status() or {}

    def stop(self) -> str | None:
        if not self.active:
            return None
        assert self._file is not None and self.path is not None
        self._file.close()
        self._file = self._writer = None
        return _shown(self.path)

    def on_packet(self, pkt: dict[str, Any]) -> None:
        if not self.active:
            return
        n = len(pkt["pressure"])
        for k in range(n):
            self._writer.writerow((pkt["device"], pkt["seq"], k, pkt["t0_ms"],
                                   *(pkt[c][k] for c in CHANNELS), self.label))
        self.samples += n
