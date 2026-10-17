"""opencode2api — lifecycle plugin for the OpenCode Zen relay.

Runs the `opencode2api` relay (https://github.com/6Kmfi6HP/opencode2api, pinned
release, SHA256-verified) as a **child of the Hermes process**: it starts when
Hermes loads the plugin and dies with the Hermes process that spawned it
(atexit + PR_SET_PDEATHSIG from a keeper thread). The gateway and
cron/kanban/send services never own a relay — closing the desktop app therefore
always closes the relay.

Model routing connects itself: on first load the plugin also appends the
`custom_providers.opencode2api` entry to `~/.hermes/config.yaml` if the route
is not configured yet (idempotent via `route_setup.py`; it never touches any
other setting). A `pre_api_request` hook guarantees the relay is up whenever a
request for that route is about to fly.
"""

from __future__ import annotations

import atexit
import logging
import os
import sys
from pathlib import Path

from . import route_setup, supervisor

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).resolve().parent


def _data_root() -> Path:
    """Relay data directory. Everything (runtime binary, config, logs) lives
    inside the Hermes tree — nothing outside it."""
    raw = os.environ.get("OPENCODE2API_PLUGIN_DATA")
    if raw:
        return Path(raw)
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "plugin-data" / "opencode2api"
    except Exception:
        return _PLUGIN_DIR / "data"


def _config_path(data_root: Path) -> Path:
    return data_root / "config.json"


def _shutdown() -> None:
    try:
        supervisor.shutdown_proxy()
    except Exception:  # never let teardown raise
        logger.debug("opencode2api plugin shutdown failed", exc_info=True)


def _spawn_at_boot(data_root: Path, config_path: Path) -> None:
    """Fire-and-forget spawn: does not block plugin loading (the load deadline
    must not eat relay boot); the per-request hook waits for readiness."""
    if not supervisor.should_manage_proxy(sys.argv):
        return
    try:
        supervisor.ensure_proxy(data_root, config_path, wait=False)
    except Exception as exc:  # never block a Hermes start on relay trouble
        logger.warning("opencode2api plugin: relay spawn failed: %s", exc)


def _bootstrap() -> None:
    """Register exit coupling + boot spawn exactly once per process."""
    # Idempotent under plugin reload: a function attribute survives re-import
    # because sys.modules keeps the old module object alive.
    if getattr(_bootstrap, "_done", False):
        return
    _bootstrap._done = True

    atexit.register(_shutdown)
    if supervisor.should_manage_proxy(sys.argv):
        _spawn_at_boot(_data_root(), _config_path(_data_root()))
        # Zero-config route: append the custom_providers entry once (idempotent
        # no-op when the route already exists). Interactive surfaces only —
        # persistent services never write the user's config.
        try:
            route_setup.ensure_route()
        except Exception as exc:  # belt & braces: ensure_route is already fail-open
            logger.warning("opencode2api plugin: route setup failed: %s", exc)


def register(ctx) -> None:
    """Wired by the PluginManager once the allow-list enables the plugin."""
    _bootstrap()

    data_root = _data_root()
    config_path = _config_path(data_root)

    def _on_pre_api_request(provider: str = "", base_url: str = "", **_kwargs):
        """Revival + readiness gate for every request aimed at the relay.

        Fires inside per-attempt request assembly, BEFORE the request hits the
        wire — a relay that died mid-session is respawned in place (~1-2 s with
        the binary cached) instead of failing the turn.
        """
        try:
            if supervisor.is_relay_request(provider, base_url) and supervisor.should_manage_proxy(sys.argv):
                supervisor.ensure_proxy(data_root, config_path)
        except Exception as exc:  # fail-open: the request reports its own error
            logger.warning("opencode2api plugin: relay ensure failed: %s", exc)

    ctx.register_hook("pre_api_request", _on_pre_api_request)
    ctx.on_unload(_shutdown)
