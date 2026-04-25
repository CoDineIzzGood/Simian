"""
Screen Awareness service (Project C.H.I.M.P. / Simian).

Opt-in, on-demand screen context. This service:
    - captures the primary screen as PNG bytes (via mss + PIL),
    - resolves the active-window title (pygetwindow -> Win32 ctypes fallback),
    - optionally summarises the screenshot through a local Ollama vision
      model (default: whatever the user has set in settings.router["vision"]),
    - exposes a simple state machine: off | active | paused | degraded,
    - fails safe: missing dependencies or vision-model errors degrade the
      service rather than raising out to the GUI / chat thread.

Design goals (per Simian architecture law):
    - discoverable     (registered via core.feature_registry)
    - configurable     (settings_store keys: screen_awareness_enabled,
                                             screen_awareness_exclusions)
    - health-checkable (ScreenAwarenessService.health() -> HealthState)
    - non-blocking     (capture + analysis are sync but short; callers run
                        them from background threads via TaskRunner or
                        threading.Thread; the service never spawns its own
                        long-running loop)
    - safe to fail     (no raises; degraded state is the error surface)

Privacy / safety:
    - feature is OFF by default
    - no interval recording loop, no raw screenshot persistence
    - callers can pause/resume without disabling
    - exclusion substrings on active-window title suppress capture
"""
from __future__ import annotations

import base64
import io
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

# All heavy imports are tried lazily so an install without optional deps
# still lets the rest of Simian boot. Missing modules surface as degraded
# state at capture/analyze time, not as import errors.
try:
    import mss  # type: ignore
except Exception:  # pragma: no cover - best effort
    mss = None  # type: ignore

try:
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover
    Image = None  # type: ignore

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None  # type: ignore


# ---------------------------------------------------------------------------
# In-flight capture guard
# ---------------------------------------------------------------------------
# Concurrent "what's on my screen" requests -- e.g. the user hits the
# mic button twice while a 90s vision call is still running -- would
# otherwise each spawn another Ollama request, each competing for the
# same VRAM. The first one is usually enough; subsequent ones should
# fast-fail with a degraded snapshot so the caller can re-prompt.
# Module-level Lock (try-acquire semantics) gives us a cheap one-liner
# guard without plumbing a new arg through every call site.
_CAPTURE_INFLIGHT_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Session-level OOM model blacklist
# ---------------------------------------------------------------------------
# When a vision model fails with memory_alloc (Ollama can't allocate the
# tensors on the current hardware) we add it to _OOM_MODELS. Subsequent
# vision calls in the same session skip the heavy primary model and go
# straight to the configured lighter model (or local-context fallback)
# instead of paying the full 180s timeout + retry cycle per turn.
#
# Rationale: the latest Windows runtime logs show Ollama's memory-alloc
# failure for qwen3-vl:8b-thinking is deterministic on this box (free
# VRAM never grows mid-session without a restart). Re-trying the same
# heavy model every single call burns the user's time and produces no
# new information. The blacklist clears when the process restarts, so
# a machine upgrade / driver change doesn't need any code edit.
_OOM_MODELS: set = set()
_OOM_MODELS_LOCK = threading.Lock()


def _mark_model_oom(model: str) -> None:
    try:
        with _OOM_MODELS_LOCK:
            _OOM_MODELS.add(str(model or "").strip())
    except Exception:
        pass


def _model_is_oom(model: str) -> bool:
    try:
        with _OOM_MODELS_LOCK:
            return str(model or "").strip() in _OOM_MODELS
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class ScreenSnapshot:
    """A single captured frame + optional analysis.

    Short-lived: the service keeps at most one of these around (last) and
    never writes raw bytes to disk. Callers that need persistence must
    explicitly opt in elsewhere.
    """
    captured_at: float
    png_bytes: bytes
    width: int
    height: int
    active_window_title: Optional[str] = None
    active_app: Optional[str] = None
    analysis_text: Optional[str] = None
    vision_model: Optional[str] = None
    degraded_reason: Optional[str] = None
    # Deterministic local context derived without the vision model:
    # active window + a few other visible window titles. Always set
    # when capture succeeds, so the GUI can show *something* concrete
    # even when the vision call returns http_500/timeout/empty_response.
    local_context: Optional[str] = None


# ---------------------------------------------------------------------------
# Small helpers (pure functions so they stay testable)
# ---------------------------------------------------------------------------

def _active_window_info() -> Dict[str, Optional[str]]:
    """Best-effort active-window title and a heuristic app name.

    Tries pygetwindow first (cross-platform), falls back to the Windows
    user32 API via ctypes (no extra deps on Windows). Returns None fields
    silently when neither path works.
    """
    title: Optional[str] = None
    app: Optional[str] = None

    try:
        import pygetwindow as gw  # type: ignore
        w = gw.getActiveWindow()
        if w is not None:
            title = getattr(w, "title", None) or None
    except Exception:
        pass

    if not title:
        try:
            import ctypes  # stdlib; Win32 only usefully
            hwnd = ctypes.windll.user32.GetForegroundWindow()  # type: ignore[attr-defined]
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)  # type: ignore[attr-defined]
            if length and length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)  # type: ignore[attr-defined]
                title = buf.value or None
        except Exception:
            pass

    if title:
        # Heuristic: Windows titles usually end with " - App" or " — App".
        for sep in (" \u2014 ", " - ", " \u2013 "):
            if sep in title:
                app = title.rsplit(sep, 1)[-1].strip() or None
                break

    return {"title": title, "app": app}


def _visible_window_titles(limit: int = 8) -> List[str]:
    """Return titles of other visible windows beyond the active one.

    Best-effort: uses pygetwindow if available, falls back to an empty
    list. Filters to windows that report a non-empty title and a visible
    surface so we don't pollute the fallback with background chrome
    (tooltips, shell overlays, hidden tray windows). The result is
    deduped case-insensitively and capped at ``limit``.
    """
    titles: List[str] = []
    try:
        import pygetwindow as gw  # type: ignore
    except Exception:
        return titles
    try:
        windows = gw.getAllWindows()  # type: ignore[attr-defined]
    except Exception:
        return titles
    seen: set = set()
    for w in windows:
        try:
            title = getattr(w, "title", None) or ""
            if not title.strip():
                continue
            if getattr(w, "isMinimized", False):
                continue
            # Some backends expose .visible; treat absence as "probably visible".
            if getattr(w, "visible", True) is False:
                continue
            key = title.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            titles.append(title.strip())
            if len(titles) >= limit:
                break
        except Exception:
            continue
    return titles


def _build_local_context(
    title: Optional[str],
    app: Optional[str],
    other_titles: Optional[List[str]] = None,
) -> str:
    """Plain-text, vision-free context summary.

    Used directly by the GUI when the vision model times out, returns
    http_500, or is otherwise unavailable, so the user always gets
    *something* specific instead of a generic 'I failed.' message.
    """
    lines: List[str] = []
    if title:
        lines.append(f"Active window: {title}")
    if app and (not title or app.strip().lower() not in title.lower()):
        lines.append(f"Active app: {app}")
    if other_titles:
        trimmed = [t for t in other_titles if title is None or t != title]
        if trimmed:
            lines.append("Other visible windows:")
            for t in trimmed:
                lines.append(f"  - {t}")
    return "\n".join(lines)


def _is_excluded(title: Optional[str], exclusions: List[str]) -> bool:
    if not title:
        return False
    t = title.lower()
    for needle in exclusions or []:
        needle = (needle or "").strip().lower()
        if needle and needle in t:
            return True
    return False


def _capture_primary_screen_png() -> Optional[bytes]:
    """Grab the primary monitor as PNG bytes. Returns None on failure."""
    if mss is None or Image is None:
        return None
    try:
        with mss.mss() as sct:
            # monitors[0] is the virtual "all monitors" rect; monitors[1]
            # is the primary physical monitor. Keep it simple for v1.
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=False)
            return buf.getvalue()
    except Exception:
        return None


def _downscale_png_bytes(png_bytes: bytes, max_dim: int) -> bytes:
    """Return a re-encoded PNG whose longer side is <= ``max_dim``.

    Falls back to the input bytes on any failure (missing Pillow, decode
    error, zero-size input, ...). A no-op when ``max_dim`` is 0, negative,
    or the image is already smaller than the limit. The point is purely
    to cut the base64 payload that gets shipped to the vision model: a
    4K screenshot is ~8 MB of base64 and dominates the vision call
    budget on slower hardware for no visible quality gain on text-heavy
    UI screenshots.
    """
    if Image is None or not png_bytes or max_dim is None or max_dim <= 0:
        return png_bytes
    try:
        with Image.open(io.BytesIO(png_bytes)) as im:
            w, h = im.size
            longer = max(w, h)
            if longer <= max_dim:
                return png_bytes
            scale = float(max_dim) / float(longer)
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            # LANCZOS = highest quality downsample Pillow exposes under
            # both old (Image.LANCZOS) and new (Image.Resampling.LANCZOS)
            # APIs; fall back to a safe default if neither is present.
            resample = getattr(
                getattr(Image, "Resampling", Image), "LANCZOS", 1
            )
            resized = im.convert("RGB").resize(new_size, resample)
            buf = io.BytesIO()
            resized.save(buf, format="PNG", optimize=False)
            return buf.getvalue()
    except Exception:
        return png_bytes


def _ollama_vision_request(
    png_bytes: bytes,
    prompt: str,
    model: str,
    base_url: str = "http://127.0.0.1:11434",
    timeout: float = 180.0,
) -> "tuple[Optional[str], Optional[str]]":
    """POST image + prompt to Ollama /api/generate for a vision model.

    Returns ``(text, error_reason)`` where exactly one side is non-None:

        * ``(text, None)``   - model responded with usable response text.
        * ``(None, reason)`` - request failed; reason is one of:

            ``httpx_unavailable``   httpx module not installed
            ``no_pixels``           empty PNG bytes (nothing to analyse)
            ``ollama_unreachable``  server refused the connection
            ``timeout``             request exceeded ``timeout`` seconds
            ``model_not_installed`` 404 from Ollama (unknown model)
            ``http_<code>:<body>``  any other non-2xx status (body trimmed)
            ``bad_json``            response body wasn't valid JSON
            ``empty_response``      JSON decoded but response+thinking empty
            ``request_failed:<T>``  unexpected exception of type T

    This is deliberately verbose so the GUI can surface the concrete
    cause (``model not installed``, ``server unreachable``, ``timeout``,
    …) instead of collapsing every failure into a misleading
    ``vision_unavailable``. Thinking is explicitly disabled so thinking-
    capable vision models (e.g. ``qwen3-vl:8b-thinking``) produce
    response text within the default budget.
    """
    if httpx is None:
        return None, "httpx_unavailable"
    if not png_bytes:
        return None, "no_pixels"

    b64 = base64.b64encode(png_bytes).decode("utf-8")
    payload: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        # Disable the hidden chain-of-thought pass. On thinking-capable
        # models Ollama will otherwise route output through a long
        # ``thinking`` field and leave ``response`` empty within our
        # timeout, which previously looked like "model unavailable".
        "think": False,
        "images": [b64],
        "options": {"temperature": 0.1},
    }
    url = f"{base_url}/api/generate"

    # Split phase timeouts so a down Ollama fails fast (connect=5s) but a
    # slow thinking-capable vision model that's warming up still gets the
    # full read budget. Total wall-clock cap stays at ``timeout`` seconds
    # -- that's what the Settings tab controls.
    try:
        connect_budget = min(5.0, float(timeout))
    except Exception:
        connect_budget = 5.0
    try:
        http_timeout = httpx.Timeout(
            connect=connect_budget,
            read=float(timeout),
            write=float(timeout),
            pool=connect_budget,
        )
    except Exception:
        # Older httpx versions that don't accept phase kwargs: fall back
        # to a single total budget and accept the trade-off.
        http_timeout = float(timeout)

    try:
        r = httpx.post(url, json=payload, timeout=http_timeout)
    except httpx.ConnectError:
        return None, "ollama_unreachable"
    except httpx.TimeoutException:
        return None, "timeout"
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"request_failed:{type(exc).__name__}"

    if r.status_code == 404:
        return None, "model_not_installed"
    if r.status_code >= 400:
        body = (r.text or "").strip().replace("\n", " ")[:160]
        # Classify common server-side failure shapes so the GUI can show
        # an actionable message instead of a raw http_500. Ollama emits
        # these strings in the body when it can't load the model or
        # runs out of VRAM mid-request.
        low_body = body.lower()
        if "memory" in low_body and ("alloc" in low_body or "out of" in low_body or "layout" in low_body):
            return None, f"memory_alloc:{body}"
        if "model" in low_body and ("load" in low_body or "loading" in low_body) and ("fail" in low_body or "error" in low_body):
            return None, f"model_load_failure:{body}"
        return None, f"http_{r.status_code}:{body}"

    try:
        data = r.json()
    except Exception:
        return None, "bad_json"

    out = str(data.get("response") or "").strip()
    if not out:
        # If the server didn't honour think=False (older Ollama releases)
        # we still rescue any text it emitted into ``thinking`` rather
        # than returning a confusing empty-response failure.
        out = str(data.get("thinking") or "").strip()
    if not out:
        return None, "empty_response"
    return out, None


def _ollama_vision_warmup(
    model: str,
    base_url: str = "http://127.0.0.1:11434",
    timeout: float = 20.0,
) -> Optional[str]:
    """Cheap text-only ping that nudges Ollama to keep the vision model warm.

    Used between vision attempts when the first full-image call timed out
    or returned empty. By the time we get here Ollama has almost certainly
    already started pulling the model into VRAM -- this call just confirms
    it is now responsive and pins it there with a 5-minute ``keep_alive``.

    Returns ``None`` when the ping succeeded (model is live), or a short
    error reason string matching the vocabulary of
    ``_ollama_vision_request`` (``timeout``, ``ollama_unreachable``,
    ``model_not_installed``, ``http_<code>``, ``request_failed:<T>``).

    Intentionally narrow: no images, 1-token cap, no chain-of-thought.
    If this ping itself blows the short budget the caller can still fall
    through to a longer retry -- but logs will show the warmup failed.
    """
    if httpx is None:
        return "httpx_unavailable"
    payload: Dict[str, Any] = {
        "model": model,
        "prompt": "ok",
        "stream": False,
        "think": False,
        # Hold the model in memory for five minutes so the follow-up
        # full-image retry benefits from the warm-VRAM state.
        "keep_alive": "5m",
        "options": {"num_predict": 1, "temperature": 0.0},
    }
    url = f"{base_url}/api/generate"
    try:
        r = httpx.post(url, json=payload, timeout=float(timeout))
    except httpx.ConnectError:
        return "ollama_unreachable"
    except httpx.TimeoutException:
        return "timeout"
    except Exception as exc:  # pragma: no cover - defensive
        return f"request_failed:{type(exc).__name__}"
    if r.status_code == 404:
        return "model_not_installed"
    if r.status_code >= 400:
        body = (r.text or "").strip().replace("\n", " ")[:80]
        return f"http_{r.status_code}:{body}"
    return None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class ScreenAwarenessService:
    """Thread-safe, opt-in screen context provider.

    Lifecycle is simple on purpose: enable/disable flip the master switch,
    pause/resume gate capture without losing enabled state, and every
    capture_now() call is independent. No background thread runs by
    default.
    """

    DEFAULT_VISION_PROMPT = (
        "You are a local desktop assistant describing a screenshot for the user. "
        "Keep the answer short and factual. Include, on separate lines:\n"
        "active_app: <one line>\n"
        "window_title: <one line, omit if not visible>\n"
        "visible_summary: <1-2 sentences>\n"
        "notable_elements: <short comma-separated list>\n"
        "possible_user_intent: <short guess, or 'unclear'>\n"
        "Do not invent content that is not clearly visible."
    )

    def __init__(self, log_cb: Optional[Callable[[str], None]] = None):
        self._log = log_cb or (lambda _m: None)
        self._lock = threading.RLock()
        self._enabled: bool = False
        self._paused: bool = False
        self._last_snapshot: Optional[ScreenSnapshot] = None
        self._degraded_reason: Optional[str] = None

    # -- logging indirection so the callback can be swapped at runtime ------

    def set_log_cb(self, log_cb: Callable[[str], None]) -> None:
        self._log = log_cb or (lambda _m: None)

    def log(self, msg: str) -> None:
        try:
            self._log(msg)
        except Exception:
            pass

    # -- state machine ------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            if not self._enabled:
                self._paused = False
                self._last_snapshot = None
                self._degraded_reason = None
        self.log(f"[ScreenAwareness] Enabled: {bool(enabled)}")

    def pause(self) -> None:
        with self._lock:
            if not self._enabled:
                return
            self._paused = True
        self.log("[ScreenAwareness] Paused.")

    def resume(self) -> None:
        with self._lock:
            if not self._enabled:
                return
            self._paused = False
        self.log("[ScreenAwareness] Resumed.")

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def state(self) -> str:
        """Public state string: off | active | paused | degraded."""
        with self._lock:
            if not self._enabled:
                return "off"
            if self._degraded_reason is not None:
                return "degraded"
            if self._paused:
                return "paused"
            return "active"

    # -- capture / analyze --------------------------------------------------

    def capture_now(
        self,
        analyze: bool = False,
        vision_model: Optional[str] = None,
        exclusions: Optional[List[str]] = None,
        prompt: Optional[str] = None,
        timeout: float = 180.0,
        max_dim: int = 1280,
    ) -> Optional[ScreenSnapshot]:
        """Capture the current screen, with optional vision analysis.

        Returns None when the feature is off or paused -- callers can use
        that to decide whether to fall back to a non-screen code path.
        Returns a ScreenSnapshot with degraded_reason set when capture or
        analysis partially failed. Never raises.

        ``timeout`` is the total wall-clock budget for the vision call.
        ``max_dim`` is the longer-side pixel cap used to downscale the
        screenshot before it is shipped to Ollama (0 disables downscale).
        Both defaults are tuned for a slow first-run on a thinking-capable
        vision model. The GUI reads the authoritative values from
        ``settings_store`` and forwards them here.
        """
        with self._lock:
            if not self._enabled:
                return None
            if self._paused:
                return None

        # In-flight guard. try-acquire with a non-blocking lock so a
        # second overlapping call fast-fails instead of piling up
        # behind the slow vision round-trip.
        acquired = _CAPTURE_INFLIGHT_LOCK.acquire(blocking=False)
        if not acquired:
            self.log(
                "[ScreenAwareness] Busy: prior capture still running, "
                "returning busy degraded snapshot."
            )
            return ScreenSnapshot(
                captured_at=time.time(),
                png_bytes=b"",
                width=0,
                height=0,
                active_window_title=None,
                active_app=None,
                analysis_text=None,
                vision_model=None,
                degraded_reason="busy_prior_capture_inflight",
                local_context=None,
            )
        try:
            return self._capture_now_inner(
                analyze=analyze,
                vision_model=vision_model,
                exclusions=exclusions,
                prompt=prompt,
                timeout=timeout,
                max_dim=max_dim,
            )
        finally:
            try:
                _CAPTURE_INFLIGHT_LOCK.release()
            except Exception:
                pass

    def _capture_now_inner(
        self,
        analyze: bool = False,
        vision_model: Optional[str] = None,
        exclusions: Optional[List[str]] = None,
        prompt: Optional[str] = None,
        timeout: float = 180.0,
        max_dim: int = 1280,
    ) -> Optional[ScreenSnapshot]:
        """Real capture body. Callers go through ``capture_now`` which
        wraps this in an in-flight guard so overlapping requests can't
        pile up against the same VRAM budget."""

        # Older-hardware degradation knob. When low_resource_mode is on
        # we clamp max_dim to a sane ceiling and shrink the timeout
        # budget so a weak machine never hits the 180s worst-case path.
        # Caller values that are ALREADY tighter than the clamp win --
        # this is strictly a safety net, not an override.
        try:
            from services.settings_store import load_settings as _load_settings_low
            if bool(_load_settings_low().get("low_resource_mode", False)):
                if max_dim is None or max_dim <= 0 or max_dim > 800:
                    max_dim = 800
                if timeout is None or float(timeout) > 90.0:
                    timeout = 90.0
        except Exception:
            pass

        # Active-window metadata is safe to collect even if capture fails.
        win = _active_window_info()
        title = win.get("title")
        app = win.get("app")

        # Collect a small list of other visible windows right now so the
        # vision-failure fallback can still tell the user what apps are
        # on screen (Priority 4 in the pass brief). Cheap call; failing
        # gracefully when pygetwindow isn't available.
        try:
            other_titles = _visible_window_titles(limit=8)
        except Exception:
            other_titles = []
        local_context = _build_local_context(title, app, other_titles)

        if _is_excluded(title, exclusions or []):
            self.log(f"[ScreenAwareness] Skipped capture (excluded window: {title!r}).")
            return ScreenSnapshot(
                captured_at=time.time(),
                png_bytes=b"",
                width=0,
                height=0,
                active_window_title=title,
                active_app=app,
                degraded_reason="excluded_window",
                local_context=local_context,
            )

        png = _capture_primary_screen_png()
        degraded_reason: Optional[str] = None
        if png is None:
            degraded_reason = "capture_unavailable"
            self._set_degraded(degraded_reason)
            self.log("[ScreenAwareness] Capture unavailable (mss/PIL missing or blocked).")
            return ScreenSnapshot(
                captured_at=time.time(),
                png_bytes=b"",
                width=0,
                height=0,
                active_window_title=title,
                active_app=app,
                degraded_reason=degraded_reason,
                local_context=local_context,
            )

        w = h = 0
        if Image is not None:
            try:
                with Image.open(io.BytesIO(png)) as im:
                    w, h = im.size
            except Exception:
                pass

        # Downscale BEFORE analysis so base64 payload and model-side
        # tokenisation costs both shrink. We keep the pre-downscale w/h
        # on the snapshot so UI code continues to report the real screen
        # resolution, not the shipped-to-model resolution.
        png_to_send = _downscale_png_bytes(png, max_dim) if analyze and vision_model else png

        analysis_text: Optional[str] = None
        used_model: Optional[str] = None
        if analyze and vision_model:
            used_model = vision_model
            # Enrich the prompt with the caller-visible window list so
            # the vision model knows about overlapping windows even when
            # only the foreground one shows clearly in the captured
            # frame. This fixes the "only one window recognized" regression
            # where side-by-side apps were invisible to the assistant.
            base_prompt = prompt or self.DEFAULT_VISION_PROMPT
            if other_titles:
                win_list = "\n".join(f"- {t}" for t in other_titles[:8])
                enriched_prompt = (
                    f"{base_prompt}\n\n"
                    f"Context: the user currently has these windows open "
                    f"(foreground first):\n{win_list}\n"
                    f"If multiple are visible in the screenshot, describe them together."
                )
            else:
                enriched_prompt = base_prompt
            analysis_text, vision_error = self._run_vision_with_retry(
                png_to_send,
                enriched_prompt,
                vision_model,
                base_timeout=float(timeout) if timeout else 180.0,
            )
            if analysis_text is None:
                # Keep the concrete error reason machine-readable so the
                # GUI can render a specific message ("server unreachable",
                # "model not installed", "timeout", ...) instead of the
                # old collapsed 'vision_unavailable'.
                degraded_reason = degraded_reason or f"vision:{vision_error or 'unknown'}"
                self._set_degraded(degraded_reason)
                self.log(
                    f"[ScreenAwareness] Vision call failed ({vision_error}); "
                    f"model='{vision_model}'. Returning capture-only context."
                )
            else:
                self._clear_degraded()
        else:
            # Capture-only still counts as healthy -- vision is optional.
            self._clear_degraded()

        snap = ScreenSnapshot(
            captured_at=time.time(),
            png_bytes=png,
            width=w,
            height=h,
            active_window_title=title,
            active_app=app,
            analysis_text=analysis_text,
            vision_model=used_model,
            degraded_reason=degraded_reason,
            local_context=local_context,
        )
        with self._lock:
            self._last_snapshot = snap
        return snap

    def last_snapshot(self) -> Optional[ScreenSnapshot]:
        with self._lock:
            return self._last_snapshot

    def _run_vision_with_retry(
        self,
        png_bytes: bytes,
        prompt: str,
        model: str,
        base_timeout: float,
    ) -> "tuple[Optional[str], Optional[str]]":
        """Call the vision model with a bounded single retry on cold-start.

        Strategy (never more than two attempts, never loops):

            1. attempt 1 with ``base_timeout``
            2. if the failure reason is ``timeout`` or ``empty_response``,
               send a cheap text-only warmup ping (short budget, pins the
               model with keep_alive) then retry once with 1.5x the base
               timeout
            3. any other failure is final -- not retryable

        The honest reason code from the final attempt is returned so the
        GUI can keep distinguishing timeout / unavailable / parse errors
        instead of collapsing them. All phase transitions are logged so
        the Logs tab shows attempt 1 start, retry trigger, warmup
        result, retry start, and final reason.
        """
        try:
            t1 = float(base_timeout)
        except Exception:
            t1 = 180.0
        if t1 < 5.0:
            t1 = 5.0

        # Session OOM blacklist: if the primary model has already
        # proven it can't fit in VRAM on this machine this session,
        # skip the heavy attempt entirely and route straight to the
        # configured lighter model. Keeps the user from waiting the
        # full base_timeout every single turn.
        if _model_is_oom(model):
            try:
                from services.settings_store import load_settings as _load_settings_oom
                lighter_primary = str(
                    _load_settings_oom().get("screen_awareness_lighter_vision_model", "") or ""
                ).strip()
            except Exception:
                lighter_primary = ""
            if lighter_primary and lighter_primary != model and not _model_is_oom(lighter_primary):
                t_light = max(15.0, t1 * 0.5)
                self.log(
                    f"[ScreenAwareness] Skipping primary vision model '{model}' "
                    f"(session OOM blacklist hit); using lighter model "
                    f"'{lighter_primary}' directly (timeout={t_light:.0f}s)."
                )
                text_l, err_l = _ollama_vision_request(png_bytes, prompt, lighter_primary, timeout=t_light)
                if text_l is not None:
                    self.log("[ScreenAwareness] Lighter-model direct call succeeded.")
                    return text_l, None
                if err_l and err_l.startswith("memory_alloc"):
                    _mark_model_oom(lighter_primary)
                self.log(
                    f"[ScreenAwareness] Lighter-model direct call failed ({err_l}); "
                    "falling through to local-context fallback."
                )
                return None, (err_l or "oom_blacklist_lighter_failed")
            else:
                # No lighter model configured (or it's also blacklisted).
                # Skip vision entirely so the caller can use local context.
                self.log(
                    f"[ScreenAwareness] Primary vision model '{model}' is session-OOM "
                    "and no usable lighter model is configured; skipping vision."
                )
                return None, "oom_blacklist"

        self.log(
            f"[ScreenAwareness] Vision attempt 1/2 started "
            f"(model='{model}', timeout={t1:.0f}s)."
        )
        text, err = _ollama_vision_request(png_bytes, prompt, model, timeout=t1)
        if text is not None:
            self.log("[ScreenAwareness] Vision attempt 1 succeeded.")
            return text, None
        if err and err.startswith("memory_alloc"):
            _mark_model_oom(model)
            self.log(
                f"[ScreenAwareness] Marking model '{model}' as session-OOM; "
                "future calls this session will skip it."
            )

        # Retry shape decisions:
        #   timeout / empty_response -> warmup + retry at same size
        #   memory_alloc             -> skip warmup, aggressive downscale + retry
        #   others                   -> final, not retryable
        retry_reason = None
        if err in ("timeout", "empty_response"):
            retry_reason = "retry_warmup"
        elif err and err.startswith("memory_alloc"):
            retry_reason = "retry_downscale"
        if retry_reason is None:
            self.log(
                f"[ScreenAwareness] Vision attempt 1 failed ({err}); "
                f"non-retryable, final reason={err}."
            )
            return None, err

        if retry_reason == "retry_warmup":
            warmup_budget = max(10.0, min(30.0, t1 / 4.0))
            self.log(
                f"[ScreenAwareness] Vision attempt 1 failed ({err}); "
                f"warming model and retrying (warmup timeout {warmup_budget:.0f}s)."
            )
            warmup_err = _ollama_vision_warmup(model, timeout=warmup_budget)
            if warmup_err is None:
                self.log("[ScreenAwareness] Warmup ping ok; model is responsive.")
            else:
                # Don't bail -- the warmup being slow doesn't prove the full
                # retry will fail, and attempt 1 may already have loaded the
                # model. Log and push on.
                self.log(
                    f"[ScreenAwareness] Warmup ping failed ({warmup_err}); "
                    "retrying full vision call anyway."
                )
        else:
            # memory_alloc path: aggressively shrink the image before
            # retrying so the model has a shot at fitting in VRAM. Pick
            # 640 px as a conservative ceiling -- most UI screenshots
            # are still legible at that size and the base64 payload
            # drops by roughly 10x from a 1280 px baseline.
            try:
                original_len = len(png_bytes)
                png_bytes = _downscale_png_bytes(png_bytes, 640)
                self.log(
                    f"[ScreenAwareness] Vision attempt 1 failed ({err}); "
                    f"aggressively downscaled image "
                    f"({original_len} -> {len(png_bytes)} bytes) for retry."
                )
            except Exception as exc:
                self.log(
                    f"[ScreenAwareness] Aggressive downscale failed ({exc}); "
                    "retrying with original image."
                )

        # Retry budget multiplier is user-tunable. Default 1.0 caps the
        # retry at the same timeout instead of stretching to 1.5x --
        # protects against 180 + 270 = 450s effective waits on slow
        # machines. Clamp to a sane floor so misconfig can't set it to
        # 0 and silently turn retries into no-ops.
        try:
            from services.settings_store import load_settings as _load_settings
            factor = float(_load_settings().get("screen_awareness_retry_budget_factor", 1.0))
        except Exception:
            factor = 1.0
        if factor < 0.25:
            factor = 0.25
        t2 = t1 * factor
        self.log(
            f"[ScreenAwareness] Vision attempt 2/2 started "
            f"(model='{model}', timeout={t2:.0f}s, budget_factor={factor:.2f})."
        )
        text2, err2 = _ollama_vision_request(png_bytes, prompt, model, timeout=t2)
        if text2 is not None:
            self.log("[ScreenAwareness] Vision retry succeeded.")
            return text2, None
        self.log(
            f"[ScreenAwareness] Vision retry failed; final reason={err2}."
        )
        if err2 and err2.startswith("memory_alloc"):
            _mark_model_oom(model)
            self.log(
                f"[ScreenAwareness] Marking model '{model}' as session-OOM after retry; "
                "future calls this session will skip it."
            )

        # Final fallback: try a smaller model if the user configured one.
        # Keep this bounded by half of t1 so we don't balloon the total
        # wait. Trigger on timeout / empty_response / memory_alloc --
        # those are the three "the chosen model is too heavy or slow"
        # shapes that a model swap can actually help with.
        if err2 in ("timeout", "empty_response") or (err2 or "").startswith("memory_alloc"):
            try:
                from services.settings_store import load_settings as _load_settings
                lighter = str(_load_settings().get("screen_awareness_lighter_vision_model", "") or "").strip()
            except Exception:
                lighter = ""
            if lighter and lighter != model and not _model_is_oom(lighter):
                t3 = max(15.0, t1 * 0.5)
                self.log(
                    f"[ScreenAwareness] Vision fallback to lighter model "
                    f"'{lighter}' (timeout={t3:.0f}s)."
                )
                text3, err3 = _ollama_vision_request(png_bytes, prompt, lighter, timeout=t3)
                if text3 is not None:
                    self.log("[ScreenAwareness] Vision lighter-model fallback succeeded.")
                    return text3, None
                if err3 and err3.startswith("memory_alloc"):
                    _mark_model_oom(lighter)
                self.log(f"[ScreenAwareness] Vision lighter-model fallback failed ({err3}).")
            elif lighter and _model_is_oom(lighter):
                self.log(
                    f"[ScreenAwareness] Skipping lighter-model fallback '{lighter}' "
                    "(also session-OOM)."
                )
        return None, err2

    def _set_degraded(self, reason: str) -> None:
        with self._lock:
            self._degraded_reason = reason

    def _clear_degraded(self) -> None:
        with self._lock:
            self._degraded_reason = None

    # -- health -------------------------------------------------------------

    def health(self):
        """Return a core.health.HealthState for the feature registry."""
        # Imported lazily so this module has no hard dependency on core.*
        # at import time (keeps the service importable from scripts/tests).
        from core.health import HealthState

        with self._lock:
            enabled = self._enabled
            paused = self._paused
            reason = self._degraded_reason

        details: Dict[str, Any] = {
            "enabled": enabled,
            "paused": paused,
            "state": self.state(),
        }

        if not enabled:
            return HealthState(
                name="screen_awareness",
                status="ok",
                message="disabled",
                details=details,
            )

        if mss is None or Image is None:
            details["missing"] = "mss or PIL"
            return HealthState(
                name="screen_awareness",
                status="degraded",
                message="capture dependencies unavailable",
                details=details,
            )

        if reason is not None:
            details["degraded_reason"] = reason
            return HealthState(
                name="screen_awareness",
                status="degraded",
                message=reason,
                details=details,
            )

        return HealthState(
            name="screen_awareness",
            status="ok",
            message="paused" if paused else "active",
            details=details,
        )


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_singleton_lock = threading.Lock()
_singleton: Optional[ScreenAwarenessService] = None


def get_screen_awareness(log_cb: Optional[Callable[[str], None]] = None) -> ScreenAwarenessService:
    """Return the process-wide ScreenAwarenessService, creating it lazily.

    Safe to call from any thread. Passing log_cb only takes effect on the
    first call (or subsequent set_log_cb() calls); later callers just get
    the existing instance.
    """
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = ScreenAwarenessService(log_cb=log_cb)
        elif log_cb is not None:
            _singleton.set_log_cb(log_cb)
        return _singleton
