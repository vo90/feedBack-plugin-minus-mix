"""Self-contained client for Stem Splitter's managed local model server.

MinusMix is installed independently, so it cannot rely on an
unreleased Python service inside another plugin. It discovers the loopback
server that released Stem Splitter builds manage, speaks that server's public
HTTP API, and writes requested stems only into caller-owned temporary storage.
Remote, Docker-sidecar, and in-process engines are deliberately outside the
current MinusMix support contract.
"""
from __future__ import annotations

import json
import mimetypes
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

SUPPORTED_STEMS = ("guitar", "bass", "drums", "vocals", "piano", "other")
DEFAULT_MODEL = "bs_roformer_sw"
DEFAULT_PORT = 7865
JOB_TIMEOUT_SECONDS = 35 * 60
BUSY_RETRIES = 6
BUSY_BASE_BACKOFF = 5
BUSY_MAX_BACKOFF = 60
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


class SeparationClient:
    """Discover and use the server managed by released Stem Splitter builds."""

    def __init__(self, config_dir: Path, log, requests_module=None):
        self.config_dir = Path(config_dir)
        self.log = log
        self._requests_module = requests_module
        self._resolve_lock = threading.Lock()
        self._resolve_cache: tuple[
            float,
            tuple[ServerTarget, ...],
            tuple[ServerTarget | None, dict | None, str],
        ] | None = None

    def _requests(self):
        if self._requests_module is not None:
            return self._requests_module
        import requests
        return requests

    def _targets(self) -> list[ServerTarget]:
        splitter = _read_json(self.config_dir / "stem_splitter.json")
        state = _read_json(self.config_dir / "stem_splitter_server.json")
        model = str(splitter.get("remote_model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL

        urls: list[tuple[str, str, str | None]] = []
        state_port = _port(state.get("port"), 0)
        if state_port:
            urls.append((f"http://127.0.0.1:{state_port}", "managed-local", None))
        configured_port = _port(splitter.get("local_server_port"), DEFAULT_PORT)
        urls.append((f"http://127.0.0.1:{configured_port}", "managed-local", None))
        if configured_port != DEFAULT_PORT:
            urls.append((f"http://127.0.0.1:{DEFAULT_PORT}", "managed-local", None))

        targets: list[ServerTarget] = []
        seen: set[str] = set()
        for raw_url, kind, key in urls:
            url = _server_url(raw_url)
            if not url or url.casefold() in seen:
                continue
            seen.add(url.casefold())
            targets.append(ServerTarget(url=url, model=model, api_key=key, kind=kind))
        return targets

    def _probe(self, target: ServerTarget, timeout: float = 2.0) -> dict | None:
        response = None
        try:
            response = self._requests().get(
                f"{target.url}/health", timeout=timeout, allow_redirects=False,
            )
            if response.status_code != 200:
                return None
            value = response.json()
            return value if isinstance(value, dict) else None
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
        return str(value).strip().lower() if value is not None else None

    @staticmethod
    def _ready_reason(target: ServerTarget, health: dict) -> tuple[bool, str]:
        if str(health.get("status") or "ok").lower() not in ("ok", "ready", "healthy"):
            return False, "Stem Splitter's managed local server reported that it is not ready"
        model_state = SeparationClient._model_state(health, target.model)
        if model_state and model_state not in ("ready", "loaded", "complete", "completed"):
            friendly = model_state.replace("_", " ")
            return False, (
                f"{target.model} model is {friendly}; open Stem Splitter and finish downloading models"
            )
        device = str(health.get("device") or "").strip().upper()
        gpu = bool(health.get("gpu"))
        accelerator = f" · {device}{' GPU' if gpu and 'GPU' not in device else ''}" if device else ""
        return True, f"managed local server ready{accelerator}"

    def _resolve(
        self, targets: list[ServerTarget] | None = None,
    ) -> tuple[ServerTarget | None, dict | None, str]:
        targets = self._targets() if targets is None else targets
        target_key = tuple(targets)
        with self._resolve_lock:
            now = time.monotonic()
            if self._resolve_cache is not None:
                expires_at, cached_key, cached_result = self._resolve_cache
                if cached_key == target_key and now < expires_at:
                    return cached_result

            first_unready: tuple[dict, str] | None = None
            result: tuple[ServerTarget | None, dict | None, str] | None = None
            for target in targets:
                health = self._probe(target)
                if health is None:
                    continue
                ready, reason = self._ready_reason(target, health)
                if ready:
                    result = (target, health, reason)
                    break
                if first_unready is None:
                    first_unready = (health, reason)
            if result is None and first_unready is not None:
                health, reason = first_unready
                result = (None, health, reason)
            if result is None:
                result = (None, None, (
                    "Stem Splitter's managed local server is not running; "
                    "open Stem Splitter and start it"
                ))

            ttl = (
                READY_RESOLUTION_CACHE_SECONDS
                if result[0] is not None
                else UNREADY_RESOLUTION_CACHE_SECONDS
            )
            self._resolve_cache = (time.monotonic() + ttl, target_key, result)
            return result

    def status(self) -> dict:
        targets = self._targets()
        target, health, reason = self._resolve(targets)
        return {
            "available": bool(targets),
            "ready": target is not None,
            "engine": "server" if target else None,
            "reason": reason,
            "source": target.kind if target else None,
            "model": target.model if target else None,
            "device": health.get("device") if isinstance(health, dict) else None,
            "gpu": bool(health.get("gpu")) if isinstance(health, dict) else False,
            "supported_stems": list(SUPPORTED_STEMS),
        }

    def _get_authed(self, url: str, target: ServerTarget, *, timeout: float,
                    stream: bool = False):
        headers = {"X-API-Key": target.api_key} if target.api_key else None
        requests = self._requests()
        for _ in range(MAX_REDIRECTS + 1):
            hop_headers = headers if _same_origin(url, target.url) else None
            if headers and hop_headers is None:
                self.log.warning(
                    "minus_mix: downloading %s without the server API key "
                    "because it is off-origin from the managed local server",
                    _redact_url(url),
                )
            try:
                response = requests.get(
                    url, headers=hop_headers, timeout=timeout,
                    allow_redirects=False, stream=stream,
                )
            except Exception as exc:
                raise SeparationUnavailable(
                    "connection to Stem Splitter's managed local server was lost; "
                    "restart it and retry"
                ) from exc
            location = response.headers.get("location") if response.status_code in REDIRECT_CODES else None
            if not location:
                return response, url
            response.close()
            url = urljoin(url, location)
        raise RuntimeError(f"split server sent more than {MAX_REDIRECTS} redirects")

    def _cleanup(self, target: ServerTarget, job_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", job_id):
            return
        headers = {"X-API-Key": target.api_key} if target.api_key else None
        requests = self._requests()
        # Current managed servers expose /cache; the simpler FeedForge-compatible
        # server uses DELETE /jobs. Supporting both keeps the plugin installable
        # across current main/nightly variants without importing either project.
        for endpoint in (f"/cache/{job_id}", f"/jobs/{job_id}"):
            response = None
            try:
                response = requests.delete(
                    f"{target.url}{endpoint}", headers=headers, timeout=30,
                    allow_redirects=False,
                )
                if response.status_code in (200, 202, 204):
                    return
                if response.status_code not in (404, 405):
                    self.log.warning(
                        "minus_mix: temporary server-cache cleanup returned HTTP %s",
                        response.status_code,
                    )
                    return
            except Exception as exc:
                self.log.warning(
                    "minus_mix: temporary server-cache cleanup failed: %s", exc,
                )
                return
            finally:
                if response is not None:
                    response.close()

    def _submit(self, target: ServerTarget, mix: Path, requested: list[str],
                progress_cb: ProgressCallback, cancel_cb: CancelCallback) -> dict:
        content_type = mimetypes.guess_type(mix.name)[0] or {
            ".ogg": "audio/ogg", ".opus": "audio/opus", ".wav": "audio/wav",
            ".flac": "audio/flac", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
        }.get(mix.suffix.lower(), "application/octet-stream")
        headers = {"X-API-Key": target.api_key} if target.api_key else None
        params = {"model": target.model, "stems": ",".join(requested)}
        requests = self._requests()
        response = None
        for attempt in range(BUSY_RETRIES):
            if cancel_cb:
                cancel_cb()
            if progress_cb:
                progress_cb(
                    0.08,
                    "Uploading the full mix to Stem Splitter's managed local server",
                )
            try:
                with mix.open("rb") as handle:
                    response = requests.post(
                        f"{target.url}/separate",
                        files={"file": (mix.name, handle, content_type)},
                        params=params, headers=headers, timeout=(15, 600),
                        allow_redirects=False,
                    )
            except Exception as exc:
                raise SeparationUnavailable(
                    "could not reach Stem Splitter's managed local server; "
                    "start or restart it and retry"
                ) from exc
            if response.status_code != 503 or attempt == BUSY_RETRIES - 1:
                break
            response.close()
            wait = min(BUSY_MAX_BACKOFF, BUSY_BASE_BACKOFF * (2 ** attempt))
            if progress_cb:
                progress_cb(
                    0.10,
                    f"Stem Splitter's managed local server is busy; "
                    f"retrying in {wait} seconds",
                )
            _interruptible_wait(wait, cancel_cb)

        if response is None or response.status_code != 200:
            code = response.status_code if response is not None else "no response"
            body = _error_body(response) if response is not None else ""
            if response is not None:
                response.close()
            raise SeparationUnavailable(f"split server error ({code}): {body}")
        try:
            try:
                payload = response.json()
            except ValueError as exc:
                raise SeparationUnavailable(
                    f"split server returned a non-JSON response: {_error_body(response)}"
                ) from exc
        finally:
            response.close()
        if not isinstance(payload, dict):
            raise SeparationUnavailable("split server returned an invalid response")
        return payload

    def _poll_job(self, target: ServerTarget, payload: dict,
                  progress_cb: ProgressCallback, cancel_cb: CancelCallback
                  ) -> tuple[str | None, dict, list[str], bool]:
        raw_job_id = payload.get("job_id")
        job_id = raw_job_id if isinstance(raw_job_id, str) and raw_job_id else None
        stem_urls = payload.get("stems") if isinstance(payload.get("stems"), dict) else {}
        reported_missing = [
            str(stem).strip().lower() for stem in (payload.get("missing") or [])
            if isinstance(stem, str) and stem.strip()
        ]
        completed = bool(stem_urls)
        if stem_urls or job_id is None:
            return job_id, stem_urls, reported_missing, completed

        deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            _interruptible_wait(2.0, cancel_cb)
            response, _ = self._get_authed(
                f"{target.url}/jobs/{job_id}", target, timeout=30,
            )
            try:
                if response.status_code != 200:
                    raise SeparationUnavailable(
                        f"split server job poll failed ({response.status_code}): "
                        f"{_error_body(response)}"
                    )
                try:
                    job = response.json()
                except ValueError as exc:
                    raise SeparationUnavailable(
                        f"split server returned a non-JSON job response: {_error_body(response)}"
                    ) from exc
            finally:
                response.close()
            if not isinstance(job, dict):
                raise SeparationUnavailable("split server returned an invalid job response")
            state = str(job.get("status") or "").lower()
            if state in ("complete", "completed", "done"):
                stem_urls = job.get("stems") if isinstance(job.get("stems"), dict) else {}
                reported_missing = [
                    str(stem).strip().lower() for stem in (job.get("missing") or [])
                    if isinstance(stem, str) and stem.strip()
                ]
                return job_id, stem_urls, reported_missing, True
            if state in ("failed", "error", "canceled", "cancelled"):
                raise SeparationUnavailable(
                    f"split server job failed: {job.get('error') or state}"
                )
            raw_progress = job.get("progress")
            try:
                fraction = float(raw_progress)
                if fraction > 1:
                    fraction /= 100.0
            except (TypeError, ValueError):
                fraction = 0.35
            fraction = max(0.0, min(1.0, fraction))
            if progress_cb:
                progress_cb(
                    0.12 + fraction * 0.58,
                    f"Separating on server ({int(fraction * 100)}%)",
                )
        raise SeparationUnavailable(
            f"split server job timed out after {JOB_TIMEOUT_SECONDS // 60} minutes"
        )

    def _download_stems(self, target: ServerTarget, stem_urls: dict,
                        requested: list[str], out_dir: Path,
                        progress_cb: ProgressCallback,
                        cancel_cb: CancelCallback) -> dict[str, Path]:
        download_items = list(stem_urls.items())
        normalized = {name: _normalize_stem_id(str(name)) for name, _url in download_items}
        requested_set = set(requested)
        if requested_set.issubset({stem for stem in normalized.values() if stem}):
            download_items = [
                (name, url) for name, url in download_items
                if normalized.get(name) in requested_set
            ]

        result_dir = out_dir / "server_stems"
        result_dir.mkdir(parents=True, exist_ok=True)
        produced: dict[str, Path] = {}
        total = max(1, len(download_items))
        for index, (name, raw_url) in enumerate(download_items):
            if cancel_cb:
                cancel_cb()
            if not isinstance(raw_url, str) or not raw_url:
                continue
            url = f"{target.url}{raw_url}" if raw_url.startswith("/") else raw_url
            if progress_cb:
                progress_cb(0.72 + 0.22 * (index / total), f"Downloading {name}")
            response, final_url = self._get_authed(url, target, timeout=180, stream=True)
            destination = None
            try:
                if response.status_code != 200:
                    raise SeparationUnavailable(
                        f"stem download failed for '{name}': HTTP {response.status_code} "
                        f"from {_redact_url(final_url)}"
                    )
                clean_url = str(final_url).split("?", 1)[0].split("#", 1)[0]
                suffix = Path(clean_url).suffix.lower()
                extension = suffix if suffix in AUDIO_EXTENSIONS else ".wav"
                stem_id = _normalize_stem_id(str(name)) or _sanitize(str(name))
                destination = result_dir / f"{stem_id}{extension}"
                with destination.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if cancel_cb:
                            cancel_cb()
                        if chunk:
                            output.write(chunk)
                produced.setdefault(stem_id, destination)
            except Exception:
                if destination is not None:
                    try:
                        destination.unlink()
                    except OSError:
                        pass
                raise
            finally:
                response.close()
        return produced

    def separate(self, mix: Path, out_dir: Path, stems: tuple[str, ...],
                 progress_cb: ProgressCallback = None,
                 cancel_cb: CancelCallback = None) -> dict[str, Path]:
        requested: list[str] = []
        for value in stems:
            stem = str(value).strip().lower()
            if stem in SUPPORTED_STEMS and stem not in requested:
                requested.append(stem)
        if not requested:
            raise SeparationUnavailable("choose at least one supported instrument stem")

        target, _health, reason = self._resolve()
        if target is None:
            raise SeparationUnavailable(reason)
        if cancel_cb:
            cancel_cb()

        mix = Path(mix)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        if not mix.is_file():
            raise SeparationUnavailable("the temporary full-mix audio file is missing")

        if progress_cb:
            progress_cb(0.05, "Connecting to Stem Splitter's managed local server")
        payload = self._submit(target, mix, requested, progress_cb, cancel_cb)

        job_id, stem_urls, reported_missing, completed = self._poll_job(
            target, payload, progress_cb, cancel_cb,
        )
        if not stem_urls:
            if completed and isinstance(job_id, str):
                self._cleanup(target, job_id)
            if reported_missing:
                raise SeparationUnavailable(
                    "the selected model did not produce: " + ", ".join(reported_missing)
                )
            raise SeparationUnavailable("split server returned no stems")

        try:
            produced = self._download_stems(
                target, stem_urls, requested, out_dir, progress_cb, cancel_cb,
            )
        finally:
            if completed and job_id is not None:
                self._cleanup(target, job_id)

        missing = [stem for stem in requested if stem not in produced]
        if missing:
            raise SeparationUnavailable(
                "the selected model did not produce: " + ", ".join(missing)
            )
        if progress_cb:
            progress_cb(1.0, "Temporary stem download complete")
        return {stem: produced[stem] for stem in requested}
