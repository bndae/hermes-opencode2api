"""Lifecycle supervisor for the opencode2api relay plugin.

Owns the whole relay lifecycle inside Hermes: binary install (official GitHub
release + SHA256), config bootstrap, spawn/adopt semantics on its own port, and
death-coupling to the Hermes process via ``PR_SET_PDEATHSIG`` (POSIX) or
``CREATE_NO_WINDOW`` (Windows; graceful close on exit kernel coupling is
best-effort per platform). Only the process that spawned the relay stops it;
adopters leave it alone.

Port is deliberately 8001, not the systemd proxy's former 8000: the plugin owns
its own port from day one, so the cutover never requires guessing who listens,
and a stale 8000 relay from the previous lifecycle can never collide with a
live one the plugin just spawned.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Sequence

VERSION = "0.14.38"
REPOSITORY = "6Kmfi6HP/opencode2api"
# The relay's historical port (the systemd unit served 8000 too). Keeping it
# means the cutover needs NO config.yaml edit and NO model re-selection: the
# session's persisted /model override (custom:opencode2api → 127.0.0.1:8000/v1)
# stays valid when the plugin replaces the unit.
PORT = 8000
HEALTH_URL = f"http://127.0.0.1:{PORT}/health"
# How long a freshly spawned child may take to answer /health before the keeper
# treats it as wedged and replaces it.
_STARTUP_GRACE_SECONDS = 30.0

# Mirrors the tuned relay config from the pre-plugin deployment (relay-side
# prompt-cache knobs off — that upstream rejects them — plus free-tier caps).
DEFAULT_CONFIG: dict = {
    "model_alias": [
        {"keyword": "deepseek-v4-flash", "target": "deepseek-v4-flash-free",
         "match_type": "exact", "case_insensitive": False, "enabled": True},
        {"keyword": "laguna-s-2.1", "target": "laguna-s-2.1-free",
         "match_type": "exact", "case_insensitive": False, "enabled": True},
        {"keyword": "ling-3.0-flash", "target": "ling-3.0-flash-free",
         "match_type": "exact", "case_insensitive": False, "enabled": True},
        {"keyword": "mimo-v2.5", "target": "mimo-v2.5-free",
         "match_type": "exact", "case_insensitive": False, "enabled": True},
        {"keyword": "nemotron-3-ultra", "target": "nemotron-3-ultra-free",
         "match_type": "exact", "case_insensitive": False, "enabled": True},
        {"keyword": "north-mini-code", "target": "north-mini-code-free",
         "match_type": "exact", "case_insensitive": False, "enabled": True},
    ],
    "reasoning_effort_map": None,
    "force_disable_thinking": False,
    "max_tokens_cap_per_model": {
        "deepseek-v4-flash-free": 131072,
        "laguna-s-2.1-free": 262144,
        "mimo-v2.5-free": 1048576,
        "nemotron-3-ultra-free": 1000000,
        "nemotron-3.5-lightning-free": 1000000,
    },
    "prompt_cache_retention": "off",
    "cache_control_breakpoints": False,
    "key_pool": {"enabled": False},
    "stream_empty_retry_max": 1,
    "stream_first_byte_timeout_ms": 30000,
}


class UnsupportedPlatform(RuntimeError):
    pass


class InvalidChecksum(RuntimeError):
    pass


def executable_name() -> str:
    return "opencode2api.exe" if os.name == "nt" else "opencode2api"


def should_manage_proxy(argv: Sequence[str]) -> bool:
    """Own the relay in every process that actually serves inference.

    Hermes 0.22.4 made the installed gateway service the surface Desktop chats
    through: its turns run as ``platform=local`` inside ``hermes-gateway.service``
    (``gateway run``), and Desktop no longer spawns a per-app ``serve`` backend.
    Excluding ``gateway`` there would leave the relay unowned and every request
    would fail to connect, so the gateway owns it like any other serving surface.

    What stays excluded: one-shot worker kinds that never serve a model request
    (``cron``/``kanban``/``serve-worker``/``daemon``/``send``) and management
    subcommands, which are short-lived and never call a model.
    """
    args = [Path(str(part)).name.lower() for part in argv]
    trailing = set(args[1:])
    if trailing & {"cron", "kanban", "serve-worker", "daemon", "send"}:
        return False
    # First real argument decides: management/inspection commands never start a proxy.
    management = {"doctor", "status", "model", "auth", "config", "plugins", "setup",
                  "dump", "verify", "update", "migrate", "pause", "resume", "logout",
                  "fallback", "secrets", "moa", "hooks", "webhook", "cron", "send",
                  "worktree", "project", "lsp", "proxy", "portal", "peer", "egress"}
    first = args[1] if len(args) > 1 else ""
    return not (first and first in management)


def _normalized_provider(provider: str) -> str:
    value = (provider or "").strip().lower()
    return value if value.startswith("custom:") else f"custom:{value}"


def _normalized_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/").lower()


def is_relay_request(provider: str, base_url: str) -> bool:
    """Match a request aimed at the relay.

    The provider NAME is the identity (no other provider is called
    custom:opencode2api), so the gate survives the port cutover; the URL, when
    present, must still point at this machine's relay ports.
    """
    if _normalized_provider(provider) != "custom:opencode2api":
        return False
    url = _normalized_url(base_url)
    if not url:
        return True
    match = re.fullmatch(r"http://(127\.0\.0\.1|localhost|\[::1\]):(8000|8001)(/.*)?", url)
    return bool(match)


def release_asset(os_name: str, architecture: str, version: str) -> str:
    os_key = {"linux": "linux", "darwin": "darwin", "windows": "windows"}.get(os_name.lower())
    arch_key = {
        "x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
    }.get(architecture.lower())
    if not os_key or not arch_key:
        raise UnsupportedPlatform(f"opencode2api has no binary for {os_name}/{architecture}")
    return f"opencode2api_v{version}_{os_key}_{arch_key}.tar.gz"


def _asset_dir(asset: str) -> str:
    """``opencode2api_v0.14.35_linux_amd64.tar.gz`` → ``opencode2api_v0.14.35_linux_amd64``."""
    return asset[:-7] if asset.endswith(".tar.gz") else asset


def checksum_for(checksums: str, asset: str) -> str:
    for line in checksums.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?\.?/?([^\s]+)", line.strip())
        if match and Path(match.group(2)).name == asset:
            return match.group(1).lower()
    raise InvalidChecksum(f"SHA256 for {asset} is missing or invalid")


def _download(url: str, destination: Path) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Hermes-opencode2api-plugin"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read(64 * 1024 * 1024 + 1)
    if len(payload) > 64 * 1024 * 1024:
        raise RuntimeError("release asset exceeds 64 MiB")
    destination.write_bytes(payload)
    return payload


def _safe_extract(archive: Path, destination: Path, expected_dir: str) -> Path:
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        root = destination.resolve()
        for member in members:
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError("release archive contains a path traversal")
            if not (member.isfile() or member.isdir()):
                raise RuntimeError("release archive contains a link or special file")
        tar.extractall(destination, members=members, filter="data")
    executable = destination / expected_dir / executable_name()
    if not executable.is_file():
        raise RuntimeError("release archive did not contain the expected executable")
    if os.name != "nt":
        executable.chmod(0o700)
    return executable


def install_binary(data_dir: Path, version: str = VERSION) -> Path:
    """Install the pinned release binary under ``data_dir/runtime/<version>/``.

    Idempotent: an existing binary is kept. The archive is downloaded,
    checksum-verified against the release's checksums.txt, extracted through
    the traversal-checked path above, and moved into place atomically so a
    half-written binary never becomes exec-able.
    """
    executable = data_dir / "runtime" / version / executable_name()
    if executable.is_file():
        return executable
    asset = release_asset(platform.system(), platform.machine(), version)
    base = f"https://github.com/{REPOSITORY}/releases/download/v{version}"
    with tempfile.TemporaryDirectory(prefix="opencode2api-") as temp:
        temp_dir = Path(temp)
        checksums = _download(f"{base}/checksums.txt", temp_dir / "checksums.txt").decode("ascii")
        expected = checksum_for(checksums, asset)
        archive = temp_dir / asset
        payload = _download(f"{base}/{asset}", archive)
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            raise InvalidChecksum(f"SHA256 mismatch for {asset}")
        stage = temp_dir / "extract"
        stage.mkdir()
        staged_binary = _safe_extract(archive, stage, _asset_dir(asset))
        executable.parent.mkdir(parents=True, exist_ok=True)
        temporary = executable.with_name(executable.name + ".new")
        shutil.copy2(staged_binary, temporary)
        if os.name != "nt":
            temporary.chmod(0o700)
        os.replace(temporary, executable)
    return executable


def ensure_config(config_path: Path) -> Path:
    """Write the tuned default relay config when none exists yet."""
    if config_path.is_file():
        return config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_name(config_path.name + ".new")
    temporary.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, config_path)
    return config_path


def healthy(timeout: float = 0.5) -> bool:
    try:
        request = urllib.request.Request(HEALTH_URL, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200 and response.read(16).strip() == b"OK"
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _preexec_pdeathsig() -> None:  # pragma: no cover - posix runtime path
    """Kernel-side death coupling: SIGTERM the relay when Hermes dies — even on SIGKILL."""
    if os.name != "posix":
        return
    import ctypes
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.prctl(1, signal.SIGTERM, 0, 0, 0)  # PR_SET_PDEATHSIG = 1
    # Race window: if the parent died before prctl, the signal already fired and
    # nobody will re-deliver it. The adopted orphan signals itself instead.
    if os.getppid() == 1:
        os.kill(os.getpid(), signal.SIGTERM)


class ProxyManager:
    """Process-local relay manager with a keeper thread.

    The relay is spawned from a dedicated long-lived thread because
    ``PR_SET_PDEATHSIG`` couples the child to the spawning **thread**, and the
    plugin-load worker dies right after ``register()`` — an inline spawn would
    be SIGTERMed ~50 ms after boot. The keeper blocks for the process lifetime,
    so kernel coupling and graceful stop both work:

    - graceful exit / plugin unload → ``stop()`` → terminate;
    - SIGKILL / crash → keeper dies with the process → kernel delivers SIGTERM;
    - relay died on its own → keeper exits → the next ``ensure_running`` respawns.
    """

    def __init__(self, data_dir: Path, config_path: Path, executable: Path | None = None):
        self.data_dir = data_dir
        self.config_path = config_path
        self.executable = executable
        self.process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._keeper: threading.Thread | None = None
        self._last_error: Exception | None = None

    def ensure_running(self, *, wait: bool = True, timeout: float = 20.0) -> bool:
        """Spawn or adopt the relay.

        Fast path (one health probe). ``wait=False`` fires the spawn without
        blocking the caller (boot path); the per-request hook always waits so
        the request never flies before the relay answers.
        """
        with self._lock:
            if self._keeper is None or not self._keeper.is_alive():
                self._stop_event.clear()
                self._last_error = None
                self._keeper = threading.Thread(
                    target=self._keeper_main, name="opencode2api-keeper", daemon=True)
                self._keeper.start()
        if not wait:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if healthy(timeout=0.3):
                return True
            with self._lock:
                if self._last_error is not None:
                    raise self._last_error
            time.sleep(0.2)
        raise TimeoutError(f"opencode2api did not become healthy within {timeout:.0f}s; see relay log")

    def _spawn_child(self, executable: Path, log_path: Path) -> subprocess.Popen:  # pragma: no cover - runtime
        """Popen the relay on THIS thread (PDEATHSIG couples to it, and the keeper
        thread is the only one that lives as long as the process)."""
        env = {
            **os.environ,
            "OPENCODE2API_CONFIG": str(self.config_path),
            "OPENCODE2API_LOG_FILE": str(log_path),
        }
        preexec = _preexec_pdeathsig if os.name == "posix" else None
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        with log_path.open("ab", buffering=0) as log:
            return subprocess.Popen(
                [
                    str(executable),
                    "-port", str(PORT),
                    "-config", str(self.config_path),
                    "-password", "",
                    "-log-file", str(log_path),
                    "-log-stdout=false",
                ],
                cwd=str(self.data_dir), env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT,
                preexec_fn=preexec, creationflags=creationflags,
            )

    def _terminate_child(self, proc: subprocess.Popen) -> None:  # pragma: no cover - runtime
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

    def _keeper_main(self) -> None:  # pragma: no cover - runtime thread path
        """Supervise the relay for the lifetime of this thread.

        The loop judges the ENDPOINT, not just our own child: a healthy relay is
        adopted (whoever spawned it), an unhealthy one with no live child is
        (re)spawned, and a child that dies on its own — SIGTERM, OOM, a port
        taken by a hand-started relay — is replaced after a bounded backoff.
        That is what makes the relay survive whatever killed it between two
        requests; before, the keeper simply gave up and nothing came back.
        """
        try:
            executable = self.executable or install_binary(self.data_dir)
            self.executable = executable
            ensure_config(self.config_path)
            log_path = self.data_dir / "logs" / "opencode2api.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            with self._lock:
                self._last_error = exc
            return

        backoff = 1.0
        spawned_at = 0.0
        while not self._stop_event.is_set():
            if healthy(timeout=0.3):
                backoff = 1.0
                with self._lock:
                    self._last_error = None
                self._stop_event.wait(1.0)
                continue
            with self._lock:
                child = self.process
            alive = child is not None and child.poll() is None
            if child is not None and alive:
                # Our child is still booting (~2s) or wedged: give it grace
                # before declaring it dead.
                if time.monotonic() - spawned_at < _STARTUP_GRACE_SECONDS:
                    self._stop_event.wait(0.5)
                    continue
                with self._lock:
                    if self.process is child:
                        self.process = None
                self._terminate_child(child)
                spawned_at = 0.0
                continue
            with self._lock:
                if self.process is child:
                    self.process = None
            try:
                proc = self._spawn_child(executable, log_path)
            except Exception as exc:
                with self._lock:
                    self._last_error = exc
                if self._stop_event.wait(min(backoff, 5.0)):
                    return
                backoff = min(backoff * 2, 30.0)
                continue
            with self._lock:
                self.process = proc
                self._last_error = None
            spawned_at = time.monotonic()
            self._stop_event.wait(0.5)
        with self._lock:
            child = self.process
            self.process = None
        if child is not None:
            self._terminate_child(child)

    def stop(self) -> None:
        with self._lock:
            keeper = self._keeper
            self._keeper = None
            process = self.process
            self.process = None
        self._stop_event.set()
        if keeper is not None and keeper is not threading.current_thread():
            keeper.join(timeout=10)
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


_MANAGERS: dict[tuple[str, str], ProxyManager] = {}
_MANAGERS_LOCK = threading.Lock()


def _manager_for(data_dir: Path, config_path: Path) -> ProxyManager:
    key = (str(data_dir), str(config_path))
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(key)
        if manager is None:
            manager = _MANAGERS[key] = ProxyManager(data_dir, config_path)
        return manager


def ensure_proxy(data_dir: Path, config_path: Path, *, wait: bool = True) -> bool:
    """Spawn or adopt the relay; safe to call on every request (fast path: one health probe).

    ``wait=False`` (boot path) fires the spawn without blocking plugin loading;
    the per-request hook then waits for readiness itself.
    """
    return _manager_for(data_dir, config_path).ensure_running(wait=wait)


def shutdown_proxy() -> None:
    """Stop every relay THIS process spawned. Foreign relays are never touched."""
    with _MANAGERS_LOCK:
        managers = list(_MANAGERS.values())
        _MANAGERS.clear()
    for manager in managers:
        manager.stop()
