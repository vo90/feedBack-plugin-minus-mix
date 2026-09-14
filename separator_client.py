"""Self-contained client for Stem Splitter's managed local model server.

MinusMix is installed independently, so it cannot rely on an
unreleased Python service inside another plugin. It discovers the loopback
server that released Stem Splitter builds manage, speaks that server's public
HTTP API, and writes requested stems only into caller-owned temporary storage.
Remote, Docker-sidecar, and in-process engines are deliberately outside the
current MinusMix support contract.
"""
from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

SUPPORTED_STEMS = ("guitar", "bass", "drums", "vocals", "piano", "other")
DEFAULT_MODEL = "bs_roformer_sw"
DEFAULT_PORT = 7865
JOB_TIMEOUT_SECONDS = 35 * 60
RECOVERY_TIMEOUT_SECONDS = 35 * 60
TOTAL_TIMEOUT_SECONDS = 70 * 60
BUSY_RETRIES = 6
BUSY_BASE_BACKOFF = 5
BUSY_MAX_BACKOFF = 60
INCOMPLETE_ATTEMPTS = 2
INCOMPLETE_RETRY_BACKOFF_SECONDS = 2
LOW_TEMP_SPACE_BYTES = 10 * 1024**3
MAX_REDIRECTS = 5
REDIRECT_CODES = (301, 302, 303, 307, 308)
MAX_ERROR_BODY = 4000
READY_RESOLUTION_CACHE_SECONDS = 5.0
UNREADY_RESOLUTION_CACHE_SECONDS = 1.0
AUDIO_EXTENSIONS = (".wav", ".flac", ".mp3", ".ogg", ".opus", ".m4a")

ProgressCallback = Callable[[float, str], None] | None
CancelCallback = Callable[[], None] | None


class SeparationUnavailable(RuntimeError):
    """The managed local server cannot currently perform a temporary split."""
    blocks_batch = False
    state = "failed"


class SeparationServiceBlocked(SeparationUnavailable):
    """The shared service needs attention; retain the remaining batch queue."""
    blocks_batch = True

    def __init__(self, message: str, state: str = "incompatible"):
        super().__init__(message)
        self.state = state


class _TransientRequest(SeparationUnavailable):
    def __init__(self, message: str, *, retry_after: float = 0, ambiguous: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.ambiguous = ambiguous


class _LostResult(SeparationUnavailable):
    pass


class IncompleteSeparationError(SeparationUnavailable):
    """A completed server job omitted one or more requested supported stems."""

    def __init__(
        self,
        missing: tuple[str, ...],
        available: tuple[str, ...],
        *,
        attempts: int,
        temp_free_bytes: int | None,
    ):
        self.missing = missing
        self.available = available
        self.attempts = attempts
        self.temp_free_bytes = temp_free_bytes

        attempt_label = "attempt" if attempts == 1 else "attempts"
        stem_label = "stem" if len(missing) == 1 else "stems"
        message = (
            f"Stem Splitter completed {attempts} {attempt_label} without returning "
            f"the requested {stem_label}: {', '.join(missing)}. "
        )
        if available:
            message += (
                "Available supported stems on the final attempt: "
                f"{', '.join(available)}."
            )
        else:
            message += "No supported stems were available on the final attempt."
        if temp_free_bytes is not None and temp_free_bytes < LOW_TEMP_SPACE_BYTES:
            free_gib = temp_free_bytes / 1024**3
            message += (
                f" Only {free_gib:.1f} GiB was free on MinusMix's temporary-work volume; "
                "low temporary disk space may have contributed."
            )
        super().__init__(message)


class _TerminalJobError(SeparationUnavailable):
    """The server explicitly reported that a job reached a terminal error state."""


@dataclass(frozen=True)
class ServerTarget:
    url: str
    model: str
    api_key: str | None
    kind: str


_STEM_ALIASES = {
    "vocals": "vocals", "vocal": "vocals", "voice": "vocals",
    "drums": "drums", "drum": "drums",
    "bass": "bass",
    "guitar": "guitar", "guitars": "guitar",
    "piano": "piano", "keys": "piano", "keyboard": "piano",
    "other": "other",
    "instrumental": "other", "instruments": "other", "instrument": "other",
    "music": "other", "accompaniment": "other",
    "no_vocals": "other", "novocals": "other",
}


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _port(value, fallback: int = DEFAULT_PORT) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if 1 <= parsed <= 65535 else fallback


def _server_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        # Credentials embedded in a URL are too easy to leak through logs and
        # redirects. API keys have their own header/config field.
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        _ = parsed.port  # validate the port while parsing
    except ValueError:
        return None
    return raw


def _origin(url: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = urlsplit(str(url))
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme)
    return scheme, host, port


def _same_origin(url: str, server_url: str) -> bool:
    try:
        parsed = urlsplit(str(url))
    except ValueError:
        return False
    if not parsed.scheme and not parsed.netloc:
        return str(url).startswith("/") and not str(url).startswith("//")
    left, right = _origin(url), _origin(server_url)
    return left is not None and right is not None and left == right


def _redact_url(url: str) -> str:
    try:
        parsed = urlsplit(str(url))
    except ValueError:
        return "<unparseable url>"
    if not parsed.scheme and not parsed.netloc:
        return (parsed.path or "") + ("?…" if parsed.query else "")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" + ("?…" if parsed.query else "")


def _sanitize(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_").lower()
    return cleaned or "stem"


def _normalize_stem_id(raw_name: str) -> str | None:
    parenthesized = re.search(r"_\(([^)]+)\)_", raw_name)
    if parenthesized:
        label = re.sub(r"[^a-z0-9]+", "_", parenthesized.group(1).lower()).strip("_")
        if label in _STEM_ALIASES:
            return _STEM_ALIASES[label]
    normalized = re.sub(r"[^a-z0-9]+", "_", raw_name.lower()).strip("_")
    for alias in sorted(_STEM_ALIASES, key=len, reverse=True):
        if re.search(rf"(^|_){re.escape(alias)}(_|$)", normalized):
            return _STEM_ALIASES[alias]
    return None


def _error_body(response) -> str:
    text = response.text or ""
    if len(text) <= MAX_ERROR_BODY:
        return text.strip()
    return text[:MAX_ERROR_BODY].strip() + f"\n… [truncated, {len(text)} chars total]"


def _interruptible_wait(seconds: float, cancel_cb: CancelCallback) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if cancel_cb:
            cancel_cb()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.25, remaining))


def _valid_job_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value if 0 < len(value) <= 512 and value not in (".", "..") and not any(ord(c) < 32 for c in value) else None


def _available_supported_stems(stem_urls: dict) -> tuple[str, ...]:
    found: set[str] = set()
    for name, raw_url in stem_urls.items():
        if not isinstance(raw_url, str) or not raw_url:
            continue
        stem_id = _normalize_stem_id(str(name))
        if stem_id in SUPPORTED_STEMS:
            found.add(stem_id)
    return tuple(stem for stem in SUPPORTED_STEMS if stem in found)


def _temp_volume_free_bytes(work_dir: Path) -> int | None:
    try:
        return int(shutil.disk_usage(work_dir).free)
    except (OSError, TypeError, ValueError):
        try:
            return int(shutil.disk_usage(tempfile.gettempdir()).free)
        except (OSError, TypeError, ValueError):
            return None


def _validate_audio(path: Path, cancel_cb: CancelCallback = None) -> bool:
    """Decode with the host's existing FFmpeg; no ML/runtime dependency imports."""
    from audio import _ffmpeg_cmd
    ffmpeg = _ffmpeg_cmd()
    if not ffmpeg:
        raise SeparationServiceBlocked("FFmpeg is unavailable; repair the game installation")
    flags = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(
            [ffmpeg, "-v", "error", "-xerror", "-nostdin", "-i", str(path),
             "-map", "0:a:0", "-progress", "pipe:1", "-f", "null", "-"],
            stdout=output, stderr=output, **flags,
        )
        try:
            deadline = time.monotonic() + 120
            while proc.poll() is None:
                if cancel_cb:
                    cancel_cb()
                if time.monotonic() >= deadline:
                    raise SeparationUnavailable("Downloaded audio validation timed out")
                time.sleep(.1)
            output.seek(0)
            detail = output.read().decode("utf-8", errors="replace")
            samples = re.findall(r"out_time_us=(\d+)", detail)
            return proc.returncode == 0 and any(int(value) > 0 for value in samples)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def _input_identity(path: Path, cancel_cb: CancelCallback = None) -> tuple:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            if cancel_cb:
                cancel_cb()
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, digest.digest()


class _Operation:
    """Budgets and pinned request data live for the whole separation, not a retry."""
    def __init__(self, target, requested, cancel_cb=None, state_cb=None):
        self.target = target
        self.requested = tuple(requested)
        self.cancel_cb = cancel_cb
        self.state_cb = state_cb
        self.started = time.monotonic()
        self.recovery_used = 0.0
        self.wait_since = None
        self.recomputations = 0
        self.model_engine = None
        self.model_revision = None
        self.input_identity = None
        self.configuration = None
        self.last_state = None

    def check(self):
        if self.cancel_cb:
            self.cancel_cb()
        now = time.monotonic()
        waiting = now - self.wait_since if self.wait_since is not None else 0
        recovery = self.recovery_used + waiting
        if now - self.started >= TOTAL_TIMEOUT_SECONDS or recovery >= RECOVERY_TIMEOUT_SECONDS:
            raise SeparationServiceBlocked(
                "Stem Splitter did not recover within the shared recovery time limit; "
                "check its installation and retry the waiting items", "recovery_exhausted")
        if now - self.started - recovery >= JOB_TIMEOUT_SECONDS:
            raise SeparationUnavailable(
                f"split server job timed out after {JOB_TIMEOUT_SECONDS // 60} minutes")

    def state(self, state, detail):
        self.check()
        event = {"state": state, "detail": detail}
        if self.state_cb and event != self.last_state:
            self.state_cb(event)
        self.last_state = event

    def waiting(self, detail, since=None):
        if self.wait_since is None:
            self.wait_since = time.monotonic() if since is None else since
        self.state("waiting_for_server", detail)

    def resume(self, state, detail):
        self.check()
        if self.wait_since is not None:
            self.recovery_used += time.monotonic() - self.wait_since
            self.wait_since = None
        self.state(state, detail)

    def recompute(self, detail):
        self.check()
        if self.recomputations >= INCOMPLETE_ATTEMPTS - 1:
            raise SeparationServiceBlocked(
                "The server lost the separation result again; the one recovery restart "
                "has already been used. Check Stem Splitter before retrying", "recovery_exhausted")
        self.recomputations += 1
        self.waiting(detail)


class SeparationClient:
    """Public local HTTP client with request-scoped update/restart recovery."""
    supports_state_callback = True

    def __init__(self, config_dir: Path, log, requests_module=None):
        self.config_dir = Path(config_dir)
        self.log = log
        self._requests_module = requests_module
        self._resolve_lock = threading.Lock()
        self._resolve_cache = None

    def _requests(self):
        if self._requests_module is not None:
            return self._requests_module
        import requests
        return requests

    def _configuration(self):
        splitter = _read_json(self.config_dir / "stem_splitter.json")
        state = _read_json(self.config_dir / "stem_splitter_server.json")
        configured = _port(splitter.get("local_server_port"), 0)
        recorded = _port(state.get("port"), 0)
        model = str(splitter.get("remote_model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        return configured, recorded, model

    def _targets(self) -> list[ServerTarget]:
        configured, recorded, model = self._configuration()
        port = recorded or configured or DEFAULT_PORT
        return [ServerTarget(f"http://127.0.0.1:{port}", model, None, "managed-local")]

    def _invalidate_resolution(self):
        with self._resolve_lock:
            self._resolve_cache = None

    def _probe(self, target: ServerTarget, timeout: float = 2.0) -> dict | None:
        response = None
        try:
            response = self._requests().get(
                f"{target.url}/health", timeout=timeout, allow_redirects=False)
            if response.status_code != 200:
                return {"_http_status": response.status_code, "_detail": _error_body(response)}
            value = response.json()
            return value if isinstance(value, dict) else {"_http_status": 200, "_invalid": True}
        except (ValueError, TypeError):
            return {"_http_status": 200, "_invalid": True}
        except Exception:
            return None
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def _model_state(health: dict, model: str) -> str | None:
        warmup = health.get("warmup")
        if not isinstance(warmup, dict):
            return None
        value = warmup.get(model)
        if value is None and health.get("demucs_model") == model:
            value = warmup.get("demucs")
        return str(value).strip().lower() if value is not None else None

    @staticmethod
    def _verified_inventory(runtime, target, requested):
        incompatible = {"state": "incompatible", "reason": "The local server's verified model metadata is malformed"}
        if type(runtime.get("managed")) is not bool:
            return incompatible
        if not runtime["managed"]:
            return {}
        models = runtime.get("models")
        if not isinstance(models, dict):
            return incompatible
        model = models.get(target.model)
        if model is None or (isinstance(model, dict) and model.get("verified") is not True):
            return {"state": "missing_model", "reason": f"{target.model} is not installed and verified; "
                    "open Stem Splitter and install the selected model"}
        if not isinstance(model, dict):
            return incompatible
        stems = model.get("stems")
        if not isinstance(stems, list) or not all(isinstance(stem, str) for stem in stems):
            return incompatible
        if any(key in model and not isinstance(model[key], str) for key in ("engine", "revision")):
            return incompatible
        result = {"supported_stems": [stem for stem in SUPPORTED_STEMS if stem in stems],
                  "inventory_verified": True, "model_engine": model.get("engine"),
                  "model_revision": model.get("revision")}
        missing = set(requested) - set(stems)
        if missing:
            result.update(state="unsupported_stems", reason="The selected model does not provide: " + ", ".join(sorted(missing)))
        return result

    @staticmethod
    def _assessment(target, health, requested=(), *, retrieval=False):
        result = {"ready": False, "state": "incompatible", "waitable": False,
                  "reason": "The local server has an incompatible health response; check Stem Splitter",
                  "supported_stems": list(SUPPORTED_STEMS), "inventory_verified": False}
        def outcome(state, reason, *, ready=False, waitable=False):
            return {**result, "state": state, "reason": reason, "ready": ready, "waitable": waitable}
        if health is None:
            return outcome("reconnecting", "Stem Splitter's managed local server is not running; "
                           "open Stem Splitter and start it", waitable=True)
        code = health.get("_http_status")
        if code in (429, 502, 503, 504):
            return outcome("updating", "The local server is busy or restarting", waitable=True)
        if code or health.get("_invalid"):
            return outcome("incompatible", f"Local server health returned HTTP {code}: "
                           f"{health.get('_detail') or 'invalid response'}")
        if str(health.get("status", "")).lower() not in ("ok", "ready", "healthy"):
            return result
        runtime = health.get("runtime")
        if runtime is not None and not isinstance(runtime, dict):
            return result
        if runtime is not None and type(runtime.get("schema_version")) is not int:
            return result
        recognized = isinstance(runtime, dict) and runtime.get("schema_version") == 1
        updating = False
        if recognized:
            caps = runtime.get("capabilities", [])
            activity = runtime.get("activity", {})
            if (not isinstance(caps, list) or not all(isinstance(cap, str) for cap in caps)
                    or not isinstance(activity, dict)
                    or any(key in activity and type(activity[key]) is not bool for key in ("draining", "sealed"))):
                return result
            if activity.get("draining") or activity.get("sealed"):
                updating = True
        if retrieval:
            if updating:
                return outcome("updating", "Stem Splitter is updating; waiting for its local server", waitable=True)
            return outcome("ready", "The original server is available for result retrieval", ready=True)
        model_state = SeparationClient._model_state(health, target.model)
        if model_state and (model_state.startswith("failed") or model_state in ("error", "unavailable")):
            return outcome("missing_model", f"{target.model} model failed to load; check Stem Splitter: {model_state}")
        if recognized and "verified_models_v1" in runtime.get("capabilities", []):
            inventory = SeparationClient._verified_inventory(runtime, target, requested)
            result.update(inventory)
            if "state" in inventory:
                return result
        if updating:
            return outcome("updating", "Stem Splitter is updating; waiting for its local server", waitable=True)
        if model_state in ("pending", "loading", "downloading", "warming", "initializing", "queued"):
            return outcome("warming", f"{target.model} model is {model_state}; waiting for Stem Splitter "
                           "to finish downloading models or loading them", waitable=True)
        if model_state not in (None, "skipped", "ready", "loaded", "complete", "completed"):
            return outcome("incompatible", f"Unrecognized model readiness state: {model_state}")
        if result["inventory_verified"] or model_state in ("ready", "loaded", "complete", "completed"):
            return outcome("ready", "managed local server ready", ready=True)
        return outcome("on_demand", "Local server available for an on-demand separation; model files "
                       "are not verified by this older server", ready=True)

    @staticmethod
    def _ready_reason(target, health):
        assessed = SeparationClient._assessment(target, health)
        return assessed["ready"], assessed["reason"]

    def _resolve(self, targets=None):
        targets = self._targets() if targets is None else targets
        key = tuple(targets)
        with self._resolve_lock:
            if self._resolve_cache and self._resolve_cache[1] == key and time.monotonic() < self._resolve_cache[0]:
                return self._resolve_cache[2]
        target = targets[0]
        health = self._probe(target)
        assessed = self._assessment(target, health)
        result = (target if assessed["ready"] else None, health, assessed["reason"])
        ttl = READY_RESOLUTION_CACHE_SECONDS if assessed["ready"] else UNREADY_RESOLUTION_CACHE_SECONDS
        with self._resolve_lock:
            self._resolve_cache = (time.monotonic() + ttl, key, result)
        return result

    def status(self) -> dict:
        targets = self._targets()
        _ready_target, health, _reason = self._resolve(targets)
        target = targets[0]
        assessed = self._assessment(target, health)
        configured, recorded, _model = self._configuration()
        if health is None and configured and recorded and configured != recorded:
            assessed.update(state="endpoint_changed", waitable=False,
                            reason=f"Recorded server port {recorded} differs from configured port {configured}; "
                            "start the intended server in Stem Splitter before retrying")
        if not (configured or recorded) and (health is None or health.get("_http_status")):
            assessed.update(state="unavailable", waitable=False)
            assessed["reason"] = "Stem Splitter's managed local server is not running; open Stem Splitter and install/start it"
        if configured and recorded and configured != recorded and assessed["ready"]:
            assessed["reason"] += f" on recorded port {recorded}; configured port is {configured}"
        return {"available": bool(configured or recorded or health), **assessed,
                "engine": "server" if assessed["ready"] else None,
                "source": target.kind if assessed["ready"] else None, "model": target.model,
                "device": health.get("device") if health else None,
                "gpu": bool(health.get("gpu")) if health else False}

    def _assert_pinned(self, ctx):
        configured = self._configuration()[0]
        if (self._targets()[0].url != ctx.target.url
                or (ctx.configuration is not None and configured != ctx.configuration)):
            raise SeparationServiceBlocked(
                "The configured local server endpoint changed during this export; "
                "finish server setup and retry this item", "endpoint_changed")

    def _recover(self, ctx, detail, *, retry_after=0, retrieval=False):
        ctx.waiting(detail)
        self._invalidate_resolution()
        delay = max(0.0, min(BUSY_MAX_BACKOFF, retry_after))
        while True:
            ctx.check()
            self._assert_pinned(ctx)
            if delay:
                _interruptible_wait(delay, ctx.check)
            assessed = self._assessment(ctx.target, self._probe(ctx.target), ctx.requested, retrieval=retrieval)
            ctx.check()
            if assessed["ready"]:
                if not retrieval:
                    engine = assessed.get("model_engine")
                    if ctx.model_engine and engine and ctx.model_engine != engine:
                        raise SeparationServiceBlocked("The selected model's engine contract changed; "
                                                       "review it in Stem Splitter before retrying")
                return assessed
            if not assessed["waitable"]:
                raise SeparationServiceBlocked(assessed["reason"], assessed["state"])
            ctx.waiting(assessed["reason"])
            delay = min(BUSY_MAX_BACKOFF, max(BUSY_BASE_BACKOFF, delay * 2))

    @staticmethod
    def _retry_after(response):
        raw = response.headers.get("Retry-After", response.headers.get("retry-after", 0))
        try:
            value = float(raw)
        except (ValueError, TypeError):
            try:
                value = parsedate_to_datetime(str(raw)).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                return 0
        return min(BUSY_MAX_BACKOFF, max(0.0, value)) if math.isfinite(value) else 0

    def _get_authed(self, url, target, *, timeout, stream=False):
        headers = {"X-API-Key": target.api_key} if target.api_key else None
        for _ in range(MAX_REDIRECTS + 1):
            if not _same_origin(url, target.url):
                raise SeparationServiceBlocked("The local server redirected its result to another origin; "
                                               "the export was stopped")
            url = urljoin(target.url + "/", url)
            try:
                response = self._requests().get(url, headers=headers, timeout=timeout,
                                               allow_redirects=False, stream=stream)
            except Exception as exc:
                raise _TransientRequest("Connection to the local stem server was interrupted") from exc
            location = (response.headers.get("location") or response.headers.get("Location")) if response.status_code in REDIRECT_CODES else None
            if not location:
                return response, url
            response.close()
            url = urljoin(url, location)
        raise SeparationServiceBlocked(f"split server sent more than {MAX_REDIRECTS} redirects")

    def _cleanup(self, target, job_id):
        # Results are content-addressed and may serve other clients. A returned
        # ID is not a deletion lease; let the server enforce its cache retention.
        return None

    def _submit(self, target, mix, requested, progress_cb, cancel_cb, ctx=None):
        ctx = ctx or _Operation(target, requested, cancel_cb)
        content_type = mimetypes.guess_type(mix.name)[0] or "application/octet-stream"
        headers = {"X-API-Key": target.api_key} if target.api_key else None
        while True:
            ctx.check()
            self._assert_pinned(ctx)
            identity = _input_identity(mix, ctx.check)
            if ctx.input_identity is not None and identity != ctx.input_identity:
                raise SeparationUnavailable("The temporary input changed during separation; start a new export")
            ctx.input_identity = identity
            ctx.resume("separating", "Uploading the original mix to the local stem server")
            if progress_cb:
                progress_cb(.08, "Uploading the full mix to Stem Splitter's managed local server")
            started = time.monotonic()
            try:
                with mix.open("rb") as handle:
                    response = self._requests().post(
                        f"{target.url}/separate", files={"file": (mix.name, handle, content_type)},
                        params={"model": target.model, "stems": ",".join(requested)},
                        headers=headers, timeout=(5, 60), allow_redirects=False)
            except Exception:
                ctx.recompute("Upload response was lost; waiting before the one permitted resubmission "
                              "of this input. The server may already have started its first attempt")
                ctx.wait_since = started
                self._recover(ctx, "Waiting for the same server after the lost upload response")
                continue
            try:
                if response.status_code in (429, 503):
                    delay = self._retry_after(response) or BUSY_BASE_BACKOFF
                    detail = f"Stem Splitter's managed local server is busy ({response.status_code}); waiting to retry"
                    response.close()
                    self._recover(ctx, detail, retry_after=delay)
                    continue
                if response.status_code in (502, 504):
                    ctx.recompute("The upload response was ambiguous; waiting before the one permitted resubmission")
                    ctx.wait_since = started
                    response.close()
                    self._recover(ctx, "Waiting for the same server after an ambiguous upload response")
                    continue
                if response.status_code != 200:
                    error = f"split server error ({response.status_code}): {_error_body(response)}"
                    try:
                        rejected = response.json()
                    except ValueError:
                        rejected = None
                    if isinstance(rejected, dict) and rejected.get("code") in ("model_not_installed", "unsupported_stems"):
                        state = "missing_model" if rejected["code"] == "model_not_installed" else "unsupported_stems"
                        raise SeparationServiceBlocked(error or rejected.get("error"), state)
                    if response.status_code in (401, 403, 404, 405) or (
                            response.status_code in (400, 422) and "model" in error.lower()):
                        raise SeparationServiceBlocked(error)
                    raise SeparationUnavailable(error)
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise SeparationServiceBlocked(f"split server returned a non-JSON response: {_error_body(response)}") from exc
                if not isinstance(payload, dict):
                    raise SeparationServiceBlocked("split server returned an invalid response")
                ctx.check()
                return payload
            finally:
                response.close()

    def _get_recovering(self, ctx, url, *, stream=False, label="job"):
        missing_confirmed = False
        while True:
            ctx.check()
            self._assert_pinned(ctx)
            started = time.monotonic()
            try:
                response, final_url = self._get_authed(url, ctx.target, timeout=(3, 15), stream=stream)
            except _TransientRequest:
                ctx.waiting(f"Waiting for the original server to resume {label}", since=started)
                self._recover(ctx, f"Reconnecting to retrieve the original {label}", retry_after=BUSY_BASE_BACKOFF, retrieval=True)
                continue
            if response.status_code in (429, 502, 503, 504):
                delay = self._retry_after(response) or BUSY_BASE_BACKOFF
                response.close()
                self._recover(ctx, f"Server update interrupted {label}; waiting to resume", retry_after=delay, retrieval=True)
                continue
            if response.status_code in (404, 410):
                response.close()
                self._recover(ctx, f"Checking whether the original {label} survived the restart", retrieval=True)
                if missing_confirmed:
                    raise _LostResult(f"The original {label} is no longer available on the healthy server")
                missing_confirmed = True
                continue
            if response.status_code != 200:
                detail = _error_body(response)
                code = response.status_code
                response.close()
                error = f"stem download failed for {label}: HTTP {code}" if stream else f"split server job poll failed ({code}): {detail}"
                if code in (401, 403, 405, 410, 422):
                    raise SeparationServiceBlocked(error)
                raise SeparationUnavailable(error)
            try:
                ctx.resume("downloading" if stream else "separating", f"Resuming the original {label}")
            except BaseException:
                response.close()
                raise
            return response, final_url

    @staticmethod
    def _job_result(payload):
        if not isinstance(payload, dict):
            raise SeparationServiceBlocked("split server returned an invalid job response")
        state = str(payload.get("status") or "").lower()
        if state in ("failed", "error", "canceled", "cancelled"):
            raise _TerminalJobError(f"split server job failed: {payload.get('error') or state}")
        stems = payload.get("stems") if isinstance(payload.get("stems"), dict) else {}
        completed = bool(stems) or state in ("complete", "completed", "done")
        if not completed and state not in ("", "pending", "queued", "running", "processing", "separating", "waiting"):
            raise SeparationServiceBlocked(f"split server returned an unsupported job state: {state}")
        missing = payload.get("missing") or []
        return stems, [stem for stem in missing if isinstance(stem, str)] if isinstance(missing, list) else [], completed

    def _poll_job(self, target, payload, progress_cb, cancel_cb, ctx=None):
        ctx = ctx or _Operation(target, (), cancel_cb)
        job_id = _valid_job_id(payload.get("job_id"))
        while True:
            stems, missing, complete = self._job_result(payload)
            if complete:
                return job_id, stems, missing, True
            if job_id is None:
                raise SeparationServiceBlocked("split server returned neither a result nor a valid job ID")
            ctx.check()
            _interruptible_wait(2, ctx.check)
            response, _url = self._get_recovering(ctx, f"{target.url}/jobs/{quote(job_id, safe='')}")
            try:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise SeparationServiceBlocked(f"split server returned a non-JSON job response: {_error_body(response)}") from exc
            finally:
                response.close()
            if not isinstance(payload, dict):
                raise SeparationServiceBlocked("split server returned an invalid job response")
            if payload.get("job_id") is not None and payload["job_id"] != job_id:
                raise SeparationServiceBlocked("The server returned a different job while recovering the original result")
            try:
                value = float(payload.get("progress", .35))
                value = value / 100 if value > 1 else value
                fraction = min(1., max(0., value)) if math.isfinite(value) else .35
            except (ValueError, TypeError):
                fraction = .35
            if progress_cb:
                progress_cb(.12 + fraction * .58, f"Separating on server ({int(fraction * 100)}%)")

    def _download_file(self, ctx, url, destination, label):
        partial = destination.with_name(destination.name + ".part")
        try:
            while True:
                response, _url = self._get_recovering(ctx, url, stream=True, label=repr(label))
                started = time.monotonic()
                interrupted = False
                try:
                    with partial.open("wb") as output:
                        try:
                            chunks = iter(response.iter_content(chunk_size=1024 * 1024))
                            while True:
                                ctx.check()
                                try:
                                    chunk = next(chunks)
                                except StopIteration:
                                    break
                                except Exception:
                                    interrupted = True
                                    break
                                if chunk:
                                    output.write(chunk)
                        finally:
                            response.close()
                finally:
                    response.close()
                expected = response.headers.get("Content-Length", response.headers.get("content-length"))
                if expected and str(expected).isdigit() and partial.stat().st_size != int(expected):
                    interrupted = True
                if interrupted:
                    partial.unlink(missing_ok=True)
                    ctx.waiting("Stem download was interrupted; restarting this file from byte zero", since=started)
                    self._recover(ctx, "Waiting to resume the original stem download", retry_after=BUSY_BASE_BACKOFF, retrieval=True)
                    continue
                if not partial.stat().st_size or not _validate_audio(partial, ctx.check):
                    return False
                ctx.check()
                os.replace(partial, destination)
                return True
        finally:
            partial.unlink(missing_ok=True)

    def _download_stems(self, target, stem_urls, requested, out_dir, progress_cb, cancel_cb, ctx=None):
        ctx = ctx or _Operation(target, requested, cancel_cb)
        produced = {}
        for name, raw_url in stem_urls.items():
            stem = _normalize_stem_id(str(name))
            if stem not in requested or stem in produced or not isinstance(raw_url, str) or not raw_url:
                continue
            ctx.check()
            ctx.state("downloading", f"Downloading {stem}")
            if progress_cb:
                progress_cb(.72 + .22 * len(produced) / max(1, len(requested)), f"Downloading {stem}")
            suffix = Path(urlsplit(raw_url).path).suffix.lower()
            destination = out_dir / f"{stem}{suffix if suffix in AUDIO_EXTENSIONS else '.wav'}"
            if self._download_file(ctx, urljoin(target.url + "/", raw_url), destination, stem):
                produced[stem] = destination
        return produced

    def _incomplete_error(self, requested, stem_urls, attempt, out_dir):
        available = _available_supported_stems(stem_urls)
        missing = tuple(stem for stem in requested if stem not in available)
        return IncompleteSeparationError(missing, available, attempts=attempt,
                                        temp_free_bytes=_temp_volume_free_bytes(out_dir)) if missing else None

    def _log_incomplete(self, error):
        if error.temp_free_bytes is None:
            space = "temporary-work-volume free space could not be measured"
        else:
            relation = "below" if error.temp_free_bytes < LOW_TEMP_SPACE_BYTES else "not below"
            space = (f"MinusMix temporary-work volume had {error.temp_free_bytes / 1024**3:.1f} GiB free "
                     f"({relation} the {LOW_TEMP_SPACE_BYTES / 1024**3:.0f} GiB warning threshold)")
        self.log.warning("minus_mix: incomplete separation attempt %s/%s; missing=%s; available=%s; %s",
                         error.attempts, INCOMPLETE_ATTEMPTS, ','.join(error.missing), ','.join(error.available), space)

    def separate(self, mix, out_dir, stems, progress_cb=None, cancel_cb=None, state_cb=None):
        requested = list(dict.fromkeys(str(stem).strip().lower() for stem in stems if str(stem).strip().lower() in SUPPORTED_STEMS))
        if not requested:
            raise SeparationUnavailable("choose at least one supported instrument stem")
        target = self._targets()[0]
        ctx = _Operation(target, requested, cancel_cb, state_cb)
        mix, out_dir = Path(mix), Path(out_dir)
        if not mix.is_file():
            raise SeparationUnavailable("the temporary full-mix audio file is missing")
        out_dir.mkdir(parents=True, exist_ok=True)
        ctx.configuration = self._configuration()[0]
        ctx.input_identity = _input_identity(mix, ctx.check)
        _target, health, _reason = self._resolve([target])
        assessed = self._assessment(target, health, requested)
        configured, recorded, _model = self._configuration()
        if not (configured or recorded) and (health is None or health.get("_http_status")):
            raise SeparationServiceBlocked("Stem Splitter's managed local server is not running; "
                                           "open Stem Splitter and install/start it", "unavailable")
        if health is None and configured and recorded and configured != recorded:
            raise SeparationServiceBlocked("Recorded and configured server ports differ; "
                                           "start the intended server in Stem Splitter", "endpoint_changed")
        if not assessed["ready"]:
            if not assessed["waitable"]:
                raise SeparationServiceBlocked(assessed["reason"], assessed["state"])
            assessed = self._recover(ctx, assessed["reason"])
        ctx.model_engine = assessed.get("model_engine")
        ctx.model_revision = assessed.get("model_revision")
        last_progress = 0.
        def progress(value, detail):
            nonlocal last_progress
            last_progress = max(last_progress, value)
            if progress_cb:
                progress_cb(last_progress, detail)
        while True:
            attempt_dir = None
            keep = False
            try:
                payload = self._submit(target, mix, requested, progress, cancel_cb, ctx)
                _job, urls, _missing, _complete = self._poll_job(target, payload, progress, cancel_cb, ctx)
                incomplete = self._incomplete_error(requested, urls, ctx.recomputations + 1, out_dir)
                if incomplete:
                    raise incomplete
                attempt_dir = Path(tempfile.mkdtemp(prefix="server_attempt_", dir=out_dir))
                produced = self._download_stems(target, urls, requested, attempt_dir, progress, cancel_cb, ctx)
                missing = tuple(stem for stem in requested if stem not in produced)
                if missing:
                    raise IncompleteSeparationError(missing, tuple(produced), attempts=ctx.recomputations + 1,
                                                    temp_free_bytes=_temp_volume_free_bytes(out_dir))
                ctx.check()
                progress(1., "Temporary stem download complete")
                ctx.check()
                keep = True
                return {stem: produced[stem] for stem in requested}
            except (IncompleteSeparationError, _LostResult) as exc:
                if isinstance(exc, IncompleteSeparationError):
                    self._log_incomplete(exc)
                    if ctx.recomputations >= INCOMPLETE_ATTEMPTS - 1:
                        raise
                detail = "Original separation result was lost or incomplete; retrying once with the same input and selected model. Updated model weights may be used"
                ctx.recompute(detail)
                progress(.10, detail)
                if attempt_dir is not None:
                    shutil.rmtree(attempt_dir, ignore_errors=True)
                    attempt_dir = None
                # The latest inventory is relevant only now, before recomputing.
                # Retrieving a retained old job never passes through this check.
                self._recover(ctx, detail)
            finally:
                if attempt_dir is not None and not keep:
                    shutil.rmtree(attempt_dir, ignore_errors=True)
