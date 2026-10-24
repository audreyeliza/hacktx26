"""Validator for the Pico -> Pi sensor packet (AGENTS.md section 4).

Used by the ingest path, the simulator tests and (milestone 8) captured firmware packets,
so every producer is checked against the same rules.
"""

from __future__ import annotations

import json
from typing import Any

from server.config import ContractCfg

ADC_CHANNELS = ("pressure", "pulse", "therm")
ACCEL_CHANNELS = ("ax", "ay", "az")
ARRAY_KEYS = ADC_CHANNELS + ACCEL_CHANNELS
SCALAR_KEYS = ("v", "device", "seq", "t0_ms", "dt_ms")


class PacketError(ValueError):
    """Raised when a datagram/line is not a valid sensor packet."""


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_number(x: Any) -> bool:
    return (isinstance(x, (int, float))) and not isinstance(x, bool)


def validate_packet(obj: Any, contract: ContractCfg) -> list[str]:
    """Return a list of problems with `obj`; an empty list means the packet is valid."""
    if not isinstance(obj, dict):
        return [f"packet must be a JSON object, got {type(obj).__name__}"]
    errors: list[str] = []
    missing = [k for k in SCALAR_KEYS + ARRAY_KEYS if k not in obj]
    if missing:
        errors.append(f"missing keys: {missing}")

    if "v" in obj and obj["v"] != contract.version:
        errors.append(f"v must be {contract.version}, got {obj['v']!r}")
    if "device" in obj and not (isinstance(obj["device"], str) and obj["device"]):
        errors.append("device must be a non-empty string")
    for key in ("seq", "t0_ms"):
        if key in obj and not (_is_int(obj[key]) and obj[key] >= 0):
            errors.append(f"{key} must be a non-negative integer, got {obj[key]!r}")
    if "dt_ms" in obj and obj["dt_ms"] != contract.dt_ms:
        errors.append(f"dt_ms must be {contract.dt_ms}, got {obj['dt_ms']!r}")

    lengths: dict[str, int] = {}
    for key in ARRAY_KEYS:
        arr = obj.get(key)
        if arr is None:
            continue
        if not isinstance(arr, list):
            errors.append(f"{key} must be a list")
            continue
        lengths[key] = len(arr)
        if key in ADC_CHANNELS:
            bad = [x for x in arr if not (_is_int(x) and 0 <= x <= contract.adc_max)]
            if bad:
                errors.append(f"{key} values must be ints in 0..{contract.adc_max}; bad: {bad[:3]}")
        else:
            bad = [x for x in arr if not (_is_number(x) and abs(x) <= contract.accel_max_g)]
            if bad:
                errors.append(f"{key} values must be numbers within +/-{contract.accel_max_g} g; bad: {bad[:3]}")

    if lengths:
        if len(set(lengths.values())) > 1:
            errors.append(f"arrays must have equal length, got {lengths}")
        n = next(iter(lengths.values()))
        if not 1 <= n <= contract.max_samples:
            errors.append(f"array length must be 1..{contract.max_samples}, got {n}")
    return errors


def parse_packet(data: bytes | str, contract: ContractCfg) -> dict[str, Any]:
    """Decode one datagram or serial line and validate it. Raises PacketError."""
    try:
        obj = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PacketError(f"not JSON: {exc}") from exc
    errors = validate_packet(obj, contract)
    if errors:
        raise PacketError("; ".join(errors))
    return obj


def encode_packet(packet: dict[str, Any]) -> bytes:
    """Compact JSON encoding used on the wire (key order/whitespace are not part of the contract)."""
    return json.dumps(packet, separators=(",", ":")).encode()
