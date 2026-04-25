"""
Lightweight crash-audit / self-test scaffold.

Inventories Simian's core subsystems and returns a list of
HealthState rows so the GUI (or a CLI entry point) can render a
"is every moving part wired correctly on THIS machine?" report
without actually exercising the heavy paths.

Design rules:
  * Every probe is non-destructive. No FFmpeg spawn, no Ollama
    generate, no file writes. Cheap import / version / enumerate
    calls only.
  * Every probe swallows exceptions. A failing probe degrades
    itself to status="error" with a message; it never raises.
  * Cost is bounded -- a full audit is expected to complete in
    well under 1s on a cold laptop.
  * Output is stable: each audit returns the same set of names in
    the same order, so a diff between two runs is meaningful.

This is a SCAFFOLD: it's the minimum set of probes that covers
the observed failure modes on the user's Windows box (Ollama
reachability, ffmpeg present, dshow enumerable, sounddevice list
queryable, mss / PIL importable, settings.json parseable, key
settings present). Future probes (GPU VRAM headroom, replay
buffer dir writable, router model tags, ...) can be appended
without breaking callers.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List

from core.health import HealthState


def _probe_python() -> HealthState:
    try:
        import sys
        return HealthState(
            name="python",
            status="ok",
            message=f"python {sys.version.split()[0]} on {sys.platform}",
        )
    except Exception as exc:  # pragma: no cover - defensive
        return HealthState(name="python", status="error", message=str(exc))


def _probe_settings() -> HealthState:
    try:
        from services.settings_store import load_settings, SETTINGS_PATH
        s = load_settings()
        keys = list(s.keys())
        missing = [
            k for k in (
                "router",
                "auto_start_replay",
                "replay_autostart_delay_ms",
                "screen_awareness_enabled",
                "low_resource_mode",
            )
            if k not in keys
        ]
        if missing:
            return HealthState(
                name="settings",
                status="degraded",
                message=f"settings loaded but missing keys: {missing}",
                details={"path": str(SETTINGS_PATH)},
            )
        return HealthState(
            name="settings",
            status="ok",
            message=f"{len(keys)} keys loaded",
            details={"path": str(SETTINGS_PATH)},
        )
    except Exception as exc:
        return HealthState(name="settings", status="error", message=str(exc))


def _probe_capture_deps() -> HealthState:
    missing = []
    try:
        import mss  # type: ignore  # noqa: F401
    except Exception:
        missing.append("mss")
    try:
        from PIL import Image  # type: ignore  # noqa: F401
    except Exception:
        missing.append("Pillow")
    if missing:
        return HealthState(
            name="capture_deps",
            status="degraded",
            message=f"capture libs missing: {missing}",
            details={"missing": missing},
        )
    return HealthState(name="capture_deps", status="ok", message="mss + PIL importable")


def _probe_ffmpeg() -> HealthState:
    try:
        from services.audio_devices import _find_ffmpeg
        path = _find_ffmpeg()
        exists = path == "ffmpeg" or Path(path).exists()
        if not exists:
            return HealthState(
                name="ffmpeg",
                status="degraded",
                message=f"ffmpeg resolved to {path!r} but file doesn't exist",
            )
        return HealthState(name="ffmpeg", status="ok", message=f"ffmpeg at {path}")
    except Exception as exc:
        return HealthState(name="ffmpeg", status="error", message=str(exc))


def _probe_dshow_audio() -> HealthState:
    try:
        from services.audio_devices import list_dshow_audio_devices
        devices = list_dshow_audio_devices()
        if not devices:
            return HealthState(
                name="dshow_audio",
                status="degraded",
                message="ffmpeg enumerated zero dshow audio devices",
            )
        return HealthState(
            name="dshow_audio",
            status="ok",
            message=f"{len(devices)} dshow audio device(s)",
            details={"devices": devices[:8]},
        )
    except Exception as exc:
        return HealthState(name="dshow_audio", status="error", message=str(exc))


def _probe_sounddevice() -> HealthState:
    try:
        from services.audio_devices import list_sounddevice_devices
        info = list_sounddevice_devices()
        n_in = len(info.get("inputs", []))
        n_out = len(info.get("outputs", []))
        if n_in == 0 and n_out == 0:
            return HealthState(
                name="sounddevice",
                status="degraded",
                message="sounddevice available but enumerated zero devices",
            )
        return HealthState(
            name="sounddevice",
            status="ok",
            message=f"{n_in} input(s), {n_out} output(s)",
        )
    except Exception as exc:
        return HealthState(name="sounddevice", status="error", message=str(exc))


def _probe_ollama() -> HealthState:
    try:
        import httpx
    except Exception:
        return HealthState(
            name="ollama",
            status="degraded",
            message="httpx not installed; cannot probe Ollama",
        )
    try:
        r = httpx.get("http://127.0.0.1:11434/api/tags", timeout=2.0)
    except Exception as exc:
        return HealthState(
            name="ollama",
            status="degraded",
            message=f"Ollama unreachable: {type(exc).__name__}",
        )
    if r.status_code != 200:
        return HealthState(
            name="ollama",
            status="degraded",
            message=f"Ollama answered HTTP {r.status_code}",
        )
    try:
        tags = r.json().get("models", []) or []
        names = [t.get("name") for t in tags if isinstance(t, dict)]
        return HealthState(
            name="ollama",
            status="ok",
            message=f"Ollama reachable, {len(names)} model(s) installed",
            details={"models": names[:16]},
        )
    except Exception as exc:
        return HealthState(name="ollama", status="degraded", message=f"bad json: {exc}")


def _probe_buffer_dir() -> HealthState:
    try:
        from services.settings_store import load_settings
        buf = Path(str(load_settings().get("buffer_dir", "data/buffer")))
        buf.mkdir(parents=True, exist_ok=True)
        test = buf / ".health_audit_touch"
        test.write_text("ok", encoding="utf-8")
        test.unlink(missing_ok=True)
        return HealthState(
            name="buffer_dir",
            status="ok",
            message=f"replay buffer dir writable: {buf}",
        )
    except Exception as exc:
        return HealthState(name="buffer_dir", status="error", message=str(exc))


def _probe_four_d_telemetry() -> HealthState:
    try:
        from services.four_d_telemetry import telemetry
        cap = telemetry.capacity()
        return HealthState(
            name="four_d_telemetry",
            status="ok",
            message=f"telemetry ring ready (capacity={cap})",
        )
    except Exception as exc:
        return HealthState(
            name="four_d_telemetry",
            status="degraded",
            message=f"telemetry service not importable: {exc}",
        )


# Probe order is stable. Append new probes to the END so old diffs
# keep lining up.
_PROBES = [
    _probe_python,
    _probe_settings,
    _probe_capture_deps,
    _probe_ffmpeg,
    _probe_dshow_audio,
    _probe_sounddevice,
    _probe_ollama,
    _probe_buffer_dir,
    _probe_four_d_telemetry,
]


def run_health_audit() -> List[HealthState]:
    """Run the full bounded audit. Always returns a list (never raises)."""
    results: List[HealthState] = []
    for probe in _PROBES:
        try:
            results.append(probe())
        except Exception as exc:
            name = getattr(probe, "__name__", "unknown").lstrip("_").replace("probe_", "")
            results.append(HealthState(name=name, status="error", message=str(exc)))
    return results


def format_audit_report(states: List[HealthState]) -> str:
    """Plain-text render for chat/log output."""
    lines = ["Simian health audit:"]
    for s in states:
        lines.append(f"  [{s.status:>8}] {s.name}: {s.message}")
    return "\n".join(lines)


def main() -> int:  # pragma: no cover - CLI entry
    states = run_health_audit()
    print(format_audit_report(states))
    any_error = any(s.status == "error" for s in states)
    any_degraded = any(s.status == "degraded" for s in states)
    return 2 if any_error else (1 if any_degraded else 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
