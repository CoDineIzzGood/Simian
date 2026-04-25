from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Any

from core.health import HealthState
from services.settings_store import load_settings


@dataclass
class FeatureSpec:
    key: str
    display_name: str
    settings_key: Optional[str] = None
    health_check: Optional[Callable[[], HealthState]] = None
    description: str = ""
    tags: list[str] = field(default_factory=list)


class FeatureRegistry:
    def __init__(self):
        self._features: Dict[str, FeatureSpec] = {}

    def register(self, feature: FeatureSpec) -> None:
        self._features[feature.key] = feature

    def get(self, key: str) -> Optional[FeatureSpec]:
        return self._features.get(key)

    def all(self) -> list[FeatureSpec]:
        return [self._features[k] for k in sorted(self._features.keys())]


feature_registry = FeatureRegistry()


def _screen_awareness_health() -> HealthState:
    """Deferred health probe for the screen awareness feature.

    Imported lazily so core.feature_registry remains importable even if
    optional screen-capture deps (mss/PIL/httpx) aren't installed.
    """
    try:
        from services.screen_awareness import get_screen_awareness
        return get_screen_awareness().health()
    except Exception as exc:
        return HealthState(
            name="screen_awareness",
            status="error",
            message=f"import_failed: {exc}",
        )


def _register_default_features() -> None:
    defaults = [
        FeatureSpec(key="chat", display_name="Chat", description="Local chat assistant"),
        FeatureSpec(key="clips", display_name="Clips", settings_key="auto_start_replay", description="Replay buffer and clip export"),
        FeatureSpec(key="cyber_tools", display_name="Cyber Tools", description="Security-focused utilities"),
        FeatureSpec(key="files", display_name="Files", description="Local file scanning and summarization"),
        FeatureSpec(key="image_gen", display_name="Image Generation", description="Text-to-image generation"),
        FeatureSpec(key="news", display_name="News", settings_key="news_default_category", description="RSS-backed world + tech news"),
        FeatureSpec(
            key="screen_awareness",
            display_name="Screen Awareness",
            settings_key="screen_awareness_enabled",
            health_check=_screen_awareness_health,
            description="On-demand screen context capture with optional local vision summarization",
        ),
        FeatureSpec(key="video_gen", display_name="Video Generation", description="Text-to-video generation"),
        FeatureSpec(key="world_tracker", display_name="World Tracker", description="External/world signal tracking"),
    ]
    for spec in defaults:
        feature_registry.register(spec)


def list_features() -> list[dict[str, Any]]:
    settings = load_settings()
    rows: list[dict[str, Any]] = []
    for f in feature_registry.all():
        enabled = True
        if f.settings_key:
            enabled = bool(getattr(settings, f.settings_key, False))
        rows.append(
            {
                "key": f.key,
                "name": f.display_name,
                "description": f.description,
                "settings_key": f.settings_key,
                "enabled": enabled,
                "tags": list(f.tags),
            }
        )
    return rows


def all_feature_health() -> list[HealthState]:
    states: list[HealthState] = []
    for f in feature_registry.all():
        if f.health_check is None:
            states.append(HealthState(name=f.key, status="unknown", message="No feature health check registered."))
            continue
        try:
            states.append(f.health_check())
        except Exception as exc:
            states.append(HealthState(name=f.key, status="error", message=f"feature_health_failed: {exc}"))
    return states


_register_default_features()
