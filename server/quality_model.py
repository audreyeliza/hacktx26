"""Signal-quality model v1 (AGENTS.md §7, milestone 9): the trained RandomForest, with the v0
rules as the fallback.

The model is loaded from `quality.model_path` (data/quality_model.joblib, written by
`python -m server.train_quality`). It is only activated if it loads cleanly, has the expected
shape and the same feature list as this code, and was trained on at least one recording from
the real wearable — a model trained only on simulated (or unknown-source) data is refused unless
`quality.allow_simulated_model` is set. Anything else → the v0 rules, with the reason shown on the
admin Device & data page.

Security: a .joblib file is a pickle and can run code when loaded; only the configured local path
is ever loaded (it is written by train_quality, never uploaded).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from server.config import ROOT, Config
from server.quality import window_features

log = logging.getLogger(__name__)

KNOWN_CLASSES = {"clean", "motion", "sensor_off"}


class QualityModel:
    """Holds the active model (or none). Shared by every patient's pipeline; `reload()` swaps it
    in place, so a newly trained model takes effect without restarting the server."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        path = Path(cfg.quality.model_path)
        self.path = path if path.is_absolute() else ROOT / path
        self._bundle: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self.status: dict[str, Any] = {}
        self.reload()

    @property
    def active(self) -> bool:
        return self._bundle is not None

    def _fallback(self, reason: str) -> dict[str, Any]:
        self._bundle = None
        self.status = {"active": "rules", "version": "v0", "reason": reason, "path": self._shown()}
        return self.status

    def _shown(self) -> str:
        try:
            return str(self.path.relative_to(ROOT))
        except ValueError:
            return str(self.path)

    def reload(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return self._fallback("no trained model file (python -m server.train_quality)")
            try:
                import joblib
                bundle = joblib.load(self.path)
            except Exception as exc:   # corrupt file, version mismatch, …: never take the server down
                log.warning("quality model %s failed to load: %s", self.path, exc)
                return self._fallback(f"model file failed to load ({type(exc).__name__})")
            problem = self._check(bundle)
            if problem:
                log.warning("quality model %s not used: %s", self.path, problem)
                return self._fallback(problem)
            self._bundle = bundle
            prov, metrics = bundle["provenance"], bundle.get("metrics") or {}
            self.status = {
                "active": "model", "version": f"v{bundle['version']}", "reason": None, "path": self._shown(),
                "trained_t": bundle.get("trained_t"), "n_windows": bundle.get("n_windows"),
                "classes": bundle["classes"], "class_counts": bundle.get("class_counts"),
                "accuracy": metrics.get("accuracy"), "evaluation": metrics.get("split"),
                "real_files": prov.get("real_files"), "simulated_files": prov.get("simulated_files"),
                "real_sessions": prov.get("real_sessions"),
            }
            log.info("quality model active: %s (%s windows, accuracy %s)", self._shown(),
                     bundle.get("n_windows"), metrics.get("accuracy"))
            return self.status

    def _check(self, bundle: Any) -> str | None:
        if not isinstance(bundle, dict) or not {"model", "features", "classes", "version"} <= set(bundle):
            return "model file has an unexpected format"
        if bundle["version"] != 1:
            return f"model version {bundle['version']} is not supported"
        if not set(bundle["classes"]) <= KNOWN_CLASSES or len(bundle["classes"]) < 2:
            return f"model classes {bundle['classes']} are not usable"
        ref = np.random.default_rng(0).normal(30000, 500, (6, int(self.cfg.quality.window_s * 50)))
        expected = sorted(window_features(ref, 50.0, self.cfg))
        if list(bundle["features"]) != expected:
            return "model was trained with a different feature set; retrain it"
        prov = bundle.get("provenance")
        if not isinstance(prov, dict):
            return "model has no record of where its training data came from; retrain it"
        if not prov.get("real_files") and not self.cfg.quality.allow_simulated_model:
            return "model was trained without real-wearable recordings (simulated or unknown source only)"
        try:
            bundle["model"].predict_proba(np.zeros((1, len(bundle["features"]))))
        except Exception as exc:
            return f"model failed a test prediction ({type(exc).__name__})"
        return None

    def predict(self, block: np.ndarray, fs: float) -> tuple[str, float] | None:
        """(class, probability) for one quality segment, or None (no model / unusable data)."""
        bundle = self._bundle
        if bundle is None or np.isnan(block).any():
            return None
        feats = window_features(block, fs, self.cfg)
        x = np.array([[feats[f] for f in bundle["features"]]], dtype=float)
        if not np.isfinite(x).all():
            return None
        proba = bundle["model"].predict_proba(x)[0]
        k = int(np.argmax(proba))
        return str(bundle["model"].classes_[k]), float(proba[k])
