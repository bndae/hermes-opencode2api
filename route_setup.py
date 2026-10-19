"""One-time setup of the `custom:opencode2api` route in the user's config.yaml.

The relay lifecycle is owned by :mod:`supervisor`; this module only makes sure
Hermes has a route pointing at it. On the first plugin load it appends a
`custom_providers` entry for the relay — idempotently, and never touching any
other setting. Library imports (hermes_cli.config) are late, inside the
writer, so the module itself stays stdlib-only and unit-testable.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

ROUTE_NAME = "opencode2api"
ROUTE_BASE_URL = "http://127.0.0.1:8000/v1"

# Free-tier models served by the pinned relay release at the time of the write;
# the picker still live-refreshes (`models_discovered`), this list is the
# curated fallback.
_ROUTE_MODELS = (
    "deepseek-v4.1-flash",
    "glm-5.3-flash",
    "big-pickle",
    "mimo-v2.6-flash",
    "nemotron-3.5-super-vl",
    "nemotron-3.5-lightning",
    "longcat-2.5-preview",
    "minimax-m3.1-flash",
    "jev-1.13",
    "beam",
)

ROUTE_ENTRY: Dict[str, Any] = {
    "name": ROUTE_NAME,
    "base_url": ROUTE_BASE_URL,
    "api_key": "public",
    "model": "deepseek-v4.1-flash",
    "models": {model: {} for model in _ROUTE_MODELS},
    "models_discovered": True,
}


def _list_entry_matches(entry: Any) -> bool:
    return isinstance(entry, dict) and str(entry.get("name", "")).strip().lower() == ROUTE_NAME


def route_present(config: Dict[str, Any]) -> bool:
    """True when any legacy `custom_providers:` entry (or a new-style
    `providers:` key) already points Hermes at this relay."""
    legacy = config.get("custom_providers")
    if isinstance(legacy, list) and any(_list_entry_matches(entry) for entry in legacy):
        return True
    providers = config.get("providers")
    if isinstance(providers, dict):
        for key, entry in providers.items():
            if str(key).strip().lower() != ROUTE_NAME or not isinstance(entry, dict):
                continue
            url = str(entry.get("api") or entry.get("url") or entry.get("base_url") or "")
            if url.strip().rstrip("/").startswith("http://127.0.0.1:8000"):
                return True
    return False


def ensure_route_entry(config: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Pure helper: append the route entry when absent. An existing route under
    another shape or a malformed `custom_providers:` field is left alone — the
    user's own config wins over ours."""
    if route_present(config):
        return config, False
    legacy = config.get("custom_providers")
    if legacy is None:
        config["custom_providers"] = [dict(ROUTE_ENTRY)]
        return config, True
    if not isinstance(legacy, list):
        logger.info("custom_providers is not a list; leaving config untouched")
        return config, False
    config["custom_providers"] = [*legacy, dict(ROUTE_ENTRY)]
    return config, True


def ensure_route() -> bool:
    """Load config.yaml, add the route when missing, save. Returns True when a
    change was written. Fail-open: any error (managed install, malformed YAML,
    import trouble) logs a warning and never breaks plugin loading."""
    try:
        from hermes_cli.config import load_config, save_config

        cfg = load_config()
        cfg, changed = ensure_route_entry(cfg if isinstance(cfg, dict) else {})
        if not changed:
            return False
        save_config(cfg)
        logger.info("opencode2api plugin: added custom_providers route %s", ROUTE_NAME)
        return True
    except Exception as exc:
        logger.warning("opencode2api plugin: could not add route to config.yaml: %s", exc)
        return False
