import os
import sys
import ipaddress
from typing import List, Optional

from fastapi import FastAPI, Depends, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel  # NEW

APP_NAME = "Simian API"
# Read version from env so your banner matches what you set in the shell
APP_VERSION = os.getenv("SIMIAN_API_VERSION", "1.0.0")  # CHANGED

# ---------- Env / Config ----------
SIMIAN_API_KEY = os.getenv("SIMIAN_API_KEY", "")
ALLOWED_ORIGINS_RAW = os.getenv("SIMIAN_ALLOWED_ORIGINS", "http://127.0.0.1:*,http://localhost:*")
ALLOW_NETS_RAW = os.getenv("SIMIAN_ALLOW_NETS", "127.0.0.1/32,::1/128")
SIMIAN_RPS = int(os.getenv("SIMIAN_RPS", "5"))
SIMIAN_BURST = int(os.getenv("SIMIAN_BURST", "10"))

def _expand_origins(raw: str) -> List[str]:
    out = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        if part.endswith(":*"):
            out.append(part[:-2])  # scheme://host
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

# ---------- CORS ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

# ---------- Simple API-key auth dependency ----------
SAFE_PATHS = {"/", "/healthz", "/meta", "/docs", "/openapi.json", "/redoc", "/api/health", "/api/meta"}  # CHANGED

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

# ---------- Simple loopback network guard ----------
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

# ---------- Meta ----------
@app.get("/", include_in_schema=False)
async def root():
    # Nice landing that jumps to docs
    return RedirectResponse(url="/docs")

@app.get("/healthz", tags=["meta"])
async def healthz():
    return {"ok": True}

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

# ---------- Option A: GUI compatibility shims ----------
# The GUI polls /api/health and calls /api/chat; expose those here.  :contentReference[oaicite:2]{index=2}
@app.get("/api/health", tags=["meta"])
async def api_health():
    return await healthz()

@app.get("/api/meta", tags=["meta"])
async def api_meta():
    return await meta()

class ChatIn(BaseModel):
    text: str

class ChatOut(BaseModel):
    reply: str

@app.post("/api/chat", response_model=ChatOut, dependencies=[Depends(api_key_guard)])
async def api_chat(body: ChatIn):
    # Minimal echo fallback so GUI works even without an LLM wired up.
    # Replace this stub with your real chat pipeline when ready.
    user = (body.text or "").strip()
    reply = "You said: " + user if user else "Say something and I’ll respond."
    return {"reply": reply}

# ---------- Router mounting (optional modules) ----------
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

# Ensure local package imports resolve when running from anywhere
sys.path.append(os.path.dirname(__file__))

# Mount optional feature routers if present (txt2img/video/upscale/tts)
_mount_router("routes.txt2img", "/api/gen", "gen")
_mount_router("routes.video",   "/api/gen", "gen")
_mount_router("routes.upscale", "/api/gen", "gen")
_mount_router("routes.tts",     "/api/gen", "gen")

# Mount Simian SRM router
_mount_router("routes.srm_route", "", "srm")

# Tools
_mount_router("routes.news", "/api", "news")
_mount_router("routes.files", "/api", "files")
_mount_router("routes.telemetry", "/api", "telemetry")

