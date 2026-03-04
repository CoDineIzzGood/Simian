from __future__ import annotations

from typing import Dict, Any

from services.settings_store import load_settings, save_settings
from services.ollama_client import list_models


class ModelRouterService:
    @staticmethod
    def get_routing() -> Dict[str, Any]:
        settings = load_settings()
        routing = settings.get("router", {})
        return {
            "router": routing,
            "available_models": list_models(),
        }

    @staticmethod
    def update_routing(new_router: Dict[str, str]) -> Dict[str, Any]:
        settings = load_settings()
        settings["router"] = new_router
        save_settings(settings)
        return {
            "ok": True,
            "router": new_router,
        }
