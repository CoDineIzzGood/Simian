import os
import sys
import ipaddress
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Depends, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from core.feature_registry import all_feature_health, list_features
from core.health import HealthRegistry, HealthState, rollup_status
from core.response_models import ChatIn, ChatOut, HealthComponent, HealthReport
from core.task_runner import get_task_runner
from routes.chat import chat_reply
from services.model_router import ModelRouterService

APP_NAME = "Simian API"
APP_VERSION = os.getenv("SIMIAN_API_VERSION", "1.0.0")

SIMIAN_API_KEY = os.getenv("SIMIAN_API_KEY", "")
ALLOWED_ORIGINS_RAW = os.getenv("SIMIAN_ALLOWED_ORIGINS", "http://127.0.0.1:*,http://localhost:*")
ALLOW_NETS_RAW = os.getenv("SIMIAN_ALLOW_NETS", "127.0.0.1/32,::1/128")
SIMIAN_RPS = int(os.getenv("SIMIAN_RPS", "5"))
SIMIAN_BURST = int(os.getenv("SIMIAN_BURST", "10"))

_task_runner = get_task_runner()


def _expand_origins(raw: str) -> List[str]:
    out = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        if part.endswith(":*"):
            out.append(part[:-2])
        out.append(part)
    return out


def _parse_networks(raw: str) -> List[ipaddress._BaseNetwork]:
    nets = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            pass
    return nets


ALLOWED_ORIGINS = _expand_origins(ALLOWED_ORIGINS_RAW)
ALLOW_NETS = _parse_networks(ALLOW_NETS_RAW)
app = FastAPI(title=APP_NAME, version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

SAFE_PATHS = {"/", "/healthz", "/meta", "/docs", "/openapi.json", "/redoc", "/api/health", "/api/meta"}


async def api_key_guard(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, convert_underscores=False),
):
    path = request.url.path
    if path in SAFE_PATHS or (request.method == "GET" and path.startswith("/meta")):
        return
    if not SIMIAN_API_KEY:
        return
    if not x_api_key or x_api_key != SIMIAN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def client_ip_allowed(request: Request):
    try:
        host = request.client.host
        ip = ipaddress.ip_address(host)
        return any(ip in net for net in ALLOW_NETS)
    except Exception:
        return False


@app.middleware("http")
async def _network_guard(request: Request, call_next):
    if not client_ip_allowed(request):
        return JSONResponse({"detail": "Client IP not allowed"}, status_code=403)
    return await call_next(request)


def _build_health_registry() -> HealthRegistry:
    registry = HealthRegistry()

    registry.register(
        "api",
        lambda: HealthState(name="api", status="ok", message="API process responding."),
    )

    def _chat_backend() -> HealthState:
        probe = chat_reply("health probe")
        if "Ollama" in probe and "isn't reachable" in probe:
            return HealthState(name="chat", status="degraded", message="Chat backend is offline; fallback reply active.")
        return HealthState(name="chat", status="ok", message="Chat backend reachable.")

    registry.register("chat", _chat_backend)
    return registry


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/docs")


@app.get("/healthz", tags=["meta"])
async def healthz():
    states = await _task_runner.run_blocking(_build_health_registry().run)
    report = HealthReport(
        status=rollup_status(states),
        components=[
            HealthComponent(name=s.name, status=s.status, message=s.message, details=s.details)
            for s in states
        ],
    )
    return report


@app.get("/meta", tags=["meta"])
async def meta():
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "allowed_origins": ALLOWED_ORIGINS,
        "allow_nets": [str(n) for n in ALLOW_NETS],
        "rps": SIMIAN_RPS,
        "burst": SIMIAN_BURST,
        "api_key_set": bool(SIMIAN_API_KEY),
    }


@app.get("/api/health", tags=["meta"])
async def api_health():
    return await healthz()


@app.get("/api/meta", tags=["meta"])
async def api_meta():
    return await meta()



@app.get("/api/features", tags=["meta"])
async def api_features():
    features = await _task_runner.run_blocking(list_features)
    feature_states = await _task_runner.run_blocking(all_feature_health)
    return {
        "status": rollup_status(feature_states),
        "features": features,
        "health": [
            {"name": h.name, "status": h.status, "message": h.message, "details": h.details}
            for h in feature_states
        ],
    }


@app.post("/api/chat", response_model=ChatOut, dependencies=[Depends(api_key_guard)])
async def api_chat(body: ChatIn):
    user = (body.text or body.message or "").strip()
    if not user and body.messages:
        for candidate in reversed(body.messages):
            content = str(candidate.get("content", "")).strip() if isinstance(candidate, dict) else ""
            if content:
                user = content
                break
    if not user:
        return ChatOut(reply="Say something and I’ll respond.", model=body.model)

    reply = await _task_runner.run_blocking(chat_reply, user, body.model)
    return ChatOut(reply=reply, model=body.model)


def _mount_router(module_name: str, prefix: str, tag: str):
    try:
        mod = __import__(module_name, fromlist=["router"])
        router = getattr(mod, "router", None)
        if router is None:
            print(f"[main] Module {module_name} has no 'router'")
            return
        app.include_router(router, prefix=prefix, tags=[tag], dependencies=[Depends(api_key_guard)])
        print(f"[main] Mounted {module_name} at {prefix}")
    except Exception as e:
        print(f"[main] Warning: could not mount {module_name}: {e}")


sys.path.append(os.path.dirname(__file__))

_mount_router("routes.txt2img", "/api/gen", "gen")
_mount_router("routes.video", "/api/gen", "gen")
_mount_router("routes.upscale", "/api/gen", "gen")
_mount_router("routes.tts", "/api/gen", "gen")

_mount_router("routes.srm_route", "", "srm")

_mount_router("routes.news", "/api", "news")
_mount_router("routes.files", "/api", "files")
_mount_router("routes.telemetry", "/api", "telemetry")
_mount_router("routes.model_router", "/api/models", "model-router")
