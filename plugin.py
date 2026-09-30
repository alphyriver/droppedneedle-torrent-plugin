# SPDX-License-Identifier: AGPL-3.0-only
"""DroppedNeedle v1 torrent source. Adapted from the fork at 51d8565d (NOTICE)."""

import asyncio
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import msgspec
from infrastructure.plugins.protocols import (
    DownloadMaterialization,
    DownloadTaskStatus,
    EnqueueRequest,
    IndexerResult,
    MountDiagnosis,
    PluginScoringHelper,
    PluginSearchResult,
    TaskHandle,
)
from models.common import ServiceStatus

SOURCE = "plugin:prowlarr-qbittorrent"
# Indexer API keys embedded in grab links are replaced by this marker in the stored
# payload (search candidates are returned to browsers) and restored at enqueue.
_KEY_PLACEHOLDER = "DROPPEDNEEDLE_INDEXER_KEY"


class QbtTorrentInfo(msgspec.Struct, kw_only=True):
    """One row from ``GET /api/v2/torrents/info``. ``content_path`` is the absolute
    path (in qBittorrent's namespace) of the torrent's root file/folder; ``save_path``
    is the category/save dir - the remap-prefix onto DroppedNeedle's mount."""

    hash: str = ""
    name: str = ""
    state: str = ""
    progress: float = 0.0
    size: int = 0
    downloaded: int = 0
    dlspeed: int = 0
    num_seeds: int = 0
    category: str = ""
    tags: str = ""
    content_path: str = ""
    save_path: str = ""


class QbtTorrentFile(msgspec.Struct, kw_only=True):
    """One row from ``GET /api/v2/torrents/files`` - ``name`` is the path relative
    to the save dir."""

    name: str = ""
    size: int = 0
    progress: float = 0.0


class ProwlarrSearchResult(msgspec.Struct, kw_only=True, rename="camel"):
    """One release row from ``GET /api/v1/search``. ``protocol`` discriminates
    ``usenet`` vs ``torrent``. ``download_url`` is Prowlarr's proxied grab link
    (works for both protocols); torrents may also carry ``magnet_url``."""

    guid: str = ""
    title: str = ""
    size: int = 0
    indexer_id: int = 0
    indexer: str = ""
    protocol: str = ""
    download_url: str = ""
    magnet_url: str = ""
    info_hash: str = ""
    categories: list["ProwlarrCategory"] = []
    seeders: int | None = None
    leechers: int | None = None
    grabs: int | None = None
    files: int | None = None
    publish_date: str = ""


class ProwlarrCategory(msgspec.Struct, kw_only=True, rename="camel"):
    id: int = 0
    name: str = ""


class ProwlarrIndexerInfo(msgspec.Struct, kw_only=True, rename="camel"):
    """One row from ``GET /api/v1/indexer`` (the Test-connection summary)."""

    id: int = 0
    name: str = ""
    enable: bool = True
    protocol: str = ""


class ProwlarrSystemStatus(msgspec.Struct, kw_only=True, rename="camel"):
    version: str = ""
    app_name: str = ""


class ExternalServiceError(RuntimeError):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details


logger = logging.getLogger(__name__)


class QbittorrentApiError(ExternalServiceError):
    """Transport/HTTP/qBittorrent error. Mapped to HTTP 503 by the registered handler."""

    def __init__(self, message: str, details: Any = None, *, auth: bool = False) -> None:
        super().__init__(message, details)
        self.auth = auth


class QbittorrentClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        api_key: str,
        *,
        max_attempts: int = 3,
        retry_backoff: float = 0.5,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._max_attempts = max(1, max_attempts)
        self._retry_backoff = retry_backoff

    def _url(self, path: str) -> str:
        return f"{self._base_url}/api/v2/{path.lstrip('/')}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        retry: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send a Bearer-authenticated request; retry transient failures."""
        attempts = self._max_attempts if retry else 1
        last_exc: httpx.HTTPError | None = None
        resp: httpx.Response | None = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(self._retry_backoff * (2 ** (attempt - 1)))
            try:
                resp = await self._http.request(
                    method,
                    self._url(path),
                    timeout=timeout,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                last_exc, resp = exc, None
                continue
            if resp.status_code in (401, 403):
                raise QbittorrentApiError("qBittorrent rejected the API key", auth=True)
            if resp.status_code < 500:
                return resp
        if resp is not None:
            return resp
        raise QbittorrentApiError("qBittorrent transport request failed") from last_exc

    async def version(self, *, timeout: float = 15.0) -> str:
        resp = await self._request("GET", "app/version", timeout=timeout)
        if resp.status_code >= 400:
            raise QbittorrentApiError(
                f"qBittorrent returned HTTP {resp.status_code}", details=resp.text[:200]
            )
        return resp.text.strip()

    async def add_torrent(
        self,
        *,
        urls: str,
        category: str | None = None,
        tag: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        """``POST torrents/add`` with a magnet and/or .torrent URL (newline-separated
        in ``urls``; qBittorrent fetches .torrent URLs itself, private-tracker cookies
        already embedded in Prowlarr's proxied download link). NOT retried (a re-send
        can race the first add); the endpoint returns no hash - correlate by ``tag``."""
        data: dict[str, str] = {"urls": urls}
        if category:
            data["category"] = category
        if tag:
            data["tags"] = tag
        resp = await self._request("POST", "torrents/add", data=data, timeout=timeout, retry=False)
        if resp.status_code == 415:
            raise QbittorrentApiError("qBittorrent rejected the torrent (invalid)")
        if resp.text.strip().lower().startswith("fails"):
            raise QbittorrentApiError("qBittorrent rejected the torrent")
        if resp.status_code >= 400:
            raise QbittorrentApiError(
                f"qBittorrent add returned HTTP {resp.status_code}", details=resp.text[:200]
            )

    async def torrents_info(
        self,
        *,
        category: str | None = None,
        tag: str | None = None,
        hashes: str | None = None,
        timeout: float = 30.0,
    ) -> list[QbtTorrentInfo]:
        params: dict[str, str] = {}
        if category:
            params["category"] = category
        if tag:
            params["tag"] = tag
        if hashes:
            params["hashes"] = hashes
        resp = await self._request("GET", "torrents/info", params=params, timeout=timeout)
        if resp.status_code >= 400:
            raise QbittorrentApiError(
                f"qBittorrent info returned HTTP {resp.status_code}", details=resp.text[:200]
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise QbittorrentApiError("qBittorrent returned non-JSON") from exc
        if not isinstance(data, list):
            return []
        return msgspec.convert(data, type=list[QbtTorrentInfo], strict=False)

    async def torrent_files(
        self, torrent_hash: str, *, timeout: float = 30.0
    ) -> list[QbtTorrentFile]:
        resp = await self._request(
            "GET", "torrents/files", params={"hash": torrent_hash}, timeout=timeout
        )
        if resp.status_code == 404:
            return []
        if resp.status_code >= 400:
            raise QbittorrentApiError(
                f"qBittorrent files returned HTTP {resp.status_code}", details=resp.text[:200]
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise QbittorrentApiError("qBittorrent returned non-JSON") from exc
        if not isinstance(data, list):
            return []
        return msgspec.convert(data, type=list[QbtTorrentFile], strict=False)

    async def delete_torrents(
        self, hashes: str, *, delete_files: bool, timeout: float = 30.0
    ) -> bool:
        resp = await self._request(
            "POST",
            "torrents/delete",
            data={"hashes": hashes, "deleteFiles": "true" if delete_files else "false"},
            timeout=timeout,
            retry=False,
        )
        return resp.status_code < 400


class ProwlarrApiError(ExternalServiceError):
    """Transport/HTTP/Prowlarr error. Mapped to HTTP 503 by the registered handler."""

    def __init__(self, message: str, details: Any = None, *, auth: bool = False) -> None:
        super().__init__(message, details)
        self.auth = auth


class ProwlarrClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        api_key: str,
        *,
        max_attempts: int = 3,
        retry_backoff: float = 0.5,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._max_attempts = max(1, max_attempts)
        self._retry_backoff = retry_backoff

    def _url(self, path: str) -> str:
        return f"{self._base_url}/api/v1/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._api_key}

    def absolute_url(self, value: str) -> str:
        """Resolve Prowlarr-relative result links against the configured origin."""
        return urljoin(f"{self._base_url}/", value)

    async def system_status(self, *, timeout: float = 15.0) -> ProwlarrSystemStatus:
        data = await self._get_json("system/status", timeout=timeout)
        return msgspec.convert(data, type=ProwlarrSystemStatus, strict=False)

    async def indexers(self, *, timeout: float = 15.0) -> list[ProwlarrIndexerInfo]:
        data = await self._get_json("indexer", timeout=timeout)
        if not isinstance(data, list):
            return []
        return msgspec.convert(data, type=list[ProwlarrIndexerInfo], strict=False)

    async def search(
        self,
        query: str,
        categories: list[int],
        *,
        limit: int = 100,
        timeout: float = 30.0,
    ) -> list[ProwlarrSearchResult]:
        """``GET /api/v1/search`` - Prowlarr's aggregate search across all its
        enabled indexers. ``type=search`` (free-text) is the reliable baseline:
        Prowlarr maps it onto each indexer's best-supported mode itself."""
        params: list[tuple[str, str]] = [
            ("query", query),
            ("type", "search"),
            ("limit", str(limit)),
        ]
        params.extend(("categories", str(c)) for c in categories)
        data = await self._get_json("search", params=params, timeout=timeout)
        if not isinstance(data, list):
            return []
        return msgspec.convert(data, type=list[ProwlarrSearchResult], strict=False)

    async def _get_json(
        self,
        path: str,
        *,
        params: list[tuple[str, str]] | None = None,
        timeout: float,
    ) -> Any:
        url = self._url(path)
        last_exc: httpx.HTTPError | None = None
        resp: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            if attempt:
                await asyncio.sleep(self._retry_backoff * (2 ** (attempt - 1)))
            try:
                resp = await self._http.get(
                    url, params=params, headers=self._headers(), timeout=timeout
                )
            except httpx.HTTPError as exc:
                last_exc, resp = exc, None
                continue
            if resp.status_code < 500:
                break
        if resp is None:
            raise ProwlarrApiError("Prowlarr transport request failed") from last_exc
        if resp.status_code in (401, 403):
            raise ProwlarrApiError("Prowlarr rejected the API key", auth=True)
        if resp.status_code >= 400:
            raise ProwlarrApiError(
                f"Prowlarr returned HTTP {resp.status_code}", details=resp.text[:200]
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise ProwlarrApiError("Prowlarr returned non-JSON") from exc


# Mirror of library_manager._AUDIO_SUFFIXES (kept local for layering, like SABnzbd's).
_AUDIO_SUFFIXES = {".flac", ".mp3", ".m4a", ".m4b", ".mp4", ".ogg", ".oga", ".opus", ".wav"}

_FAILED_STATES = {"error", "missingfiles"}
# Downloading-phase states that move payload bytes right now.
_ACTIVE_STATES = {"downloading", "forceddl"}
# Downloading-phase states that legitimately move 0 bytes (waiting/stalled/paused).
_WAITING_STATES = {"metadl", "stalleddl", "queueddl", "pauseddl", "stoppeddl", "allocating"}
_CHECKING_STATES = {"checkingdl", "checkingup", "checkingresumedata", "moving"}
# Post-completion (seeding/idle) states - the download itself is done.
_SEEDING_STATES = {"uploading", "stalledup", "queuedup", "pausedup", "stoppedup", "forcedup"}

# How many correlation polls to give a just-added torrent before the add is declared
# failed (magnet resolution can take a few seconds before the row appears).
_ADD_CORRELATE_ATTEMPTS = 10
_ADD_CORRELATE_DELAY = 1.0


class QbittorrentDownloadClient:
    _DIAGNOSIS_SAMPLE = 3

    def __init__(
        self,
        client: QbittorrentClient,
        url: str,
        api_key: str,
        downloads_mount: Path,
        *,
        category: str = "droppedneedle",
    ) -> None:
        self._client = client
        self._url = url
        self._api_key = api_key
        self._mount = Path(downloads_mount)
        self._category = category

    @property
    def client_name(self) -> str:
        return SOURCE

    @property
    def supported_sources(self) -> frozenset[str]:
        return frozenset({SOURCE})

    def is_configured(self) -> bool:
        return bool(self._url and self._api_key)

    async def health_check(self) -> ServiceStatus:
        try:
            version = await self._client.version()
        except Exception as exc:  # noqa: BLE001 - health check never raises
            return ServiceStatus(status="error", message=str(exc))
        if not _supports_api_key(version):
            return ServiceStatus(
                status="error",
                version=version or None,
                message="qBittorrent 5.2 or newer is required for API-key authentication",
            )
        return ServiceStatus(
            status="ok",
            version=version or None,
            message=f"qBittorrent {version}" if version else "qBittorrent",
        )

    async def enqueue(self, request: EnqueueRequest) -> TaskHandle:
        payload = json.loads(request.payload)
        urls = [payload.get("magnet_url"), payload.get("download_url")]
        urls = [url for url in urls if url]
        if not urls:
            raise QbittorrentApiError(
                "enqueue requires a magnet_uri or torrent_url for the torrent source"
            )
        correlation_id = request.job_name or f"droppedneedle-{request.task_id}"
        existing = await self._recover(request, correlation_id)
        if existing is not None:
            return self._handle(existing, correlation_id)

        last_error: QbittorrentApiError | None = None
        for url in dict.fromkeys(urls):
            try:
                await self._client.add_torrent(
                    urls=url, category=self._category, tag=correlation_id
                )
            except QbittorrentApiError as exc:
                last_error = exc
                existing = await self._recover(request, correlation_id)
                if existing is not None:
                    return self._handle(existing, correlation_id)
                # A timeout can mean the add succeeded. Do not submit a second URL.
                raise
            for _ in range(_ADD_CORRELATE_ATTEMPTS):
                existing = await self._recover(request, correlation_id)
                if existing is not None:
                    return self._handle(existing, correlation_id)
                await asyncio.sleep(_ADD_CORRELATE_DELAY)
            raise QbittorrentApiError(
                "Torrent metadata is not visible yet; retry the task to recover by tag"
            )
        if last_error is not None:
            raise last_error
        raise QbittorrentApiError("qBittorrent accepted the add but no torrent appeared")

    async def get_status(self, handle: TaskHandle) -> DownloadTaskStatus:
        info = await self._find(handle)
        if info is None:
            # Not visible yet (just-added) or removed out-of-band.
            return DownloadTaskStatus(task_id="", status="queued", matched_transfers=0)
        return _map_status(info)

    async def abort(self, handle: TaskHandle) -> bool:
        """Stop an active torrent and remove only client-owned incomplete data.

        A completed torrent is never deleted: it keeps seeding under its
        category while the host imports separate copies. Only an
        incomplete torrent is removed, WITH its partial data. A torrent that is
        already gone leaves nothing to stop, so that is success - returning False
        would wedge the cleanup journal in a retry loop."""
        return await self._remove_unless_seeding(handle, action="abort")

    async def inspect_materialization(self, handle: TaskHandle) -> DownloadMaterialization:
        """Resolve current torrent state and the local content path.

        Every return goes through ``_materialization``, which structurally cannot
        report ``file_paths`` - see its docstring for why that matters."""
        info = await self._find(handle)
        healthy = await self.downloads_mount_healthy()
        if info is None:
            return self._materialization(state="missing", mount_healthy=healthy)
        state = info.state.lower()
        if state in _FAILED_STATES:
            resolved = "failed"
        elif info.progress >= 1.0 or state in _SEEDING_STATES:
            resolved = "completed"
        else:
            resolved = "active"
        local = self._local_path(info) if info.content_path else None
        return self._materialization(
            state=resolved,
            mount_healthy=healthy,
            remote_storage=info.content_path or "",
            workspace_path=str(local) if local is not None else "",
        )

    async def discard_client_artifacts(self, handle: TaskHandle) -> bool:
        """Discard client-owned records once local cleanup is durable.

        For qBittorrent the "record" IS the live seeding session, so a completed
        torrent is retained on purpose and reported as success - there is nothing
        left that the attempt owns. An incomplete torrent has no seeding value, so
        it is removed with its partial data (same rule as ``abort``). A FAILED
        torrent reaches this without an ``abort`` first - the cleanup service only
        aborts an ``active`` one - so the removal branch is load-bearing here."""
        return await self._remove_unless_seeding(handle, action="discard")

    async def list_completed_files(self, handle: TaskHandle) -> list[Path]:
        info = await self._find(handle)
        if (
            info is None
            or info.progress < 1.0
            or not info.content_path
            or info.state.lower() in _CHECKING_STATES | _FAILED_STATES
        ):
            return []
        local = self._local_path(info)
        return await asyncio.to_thread(self._enumerate_audio, local)

    async def get_file_path(
        self, handle: TaskHandle, remote_filename: str, size: int | None = None
    ) -> Path | None:
        files = await self.list_completed_files(handle)
        basename = remote_filename.replace("\\", "/").rsplit("/", 1)[-1]
        for path in files:
            if path.name == basename:
                return path
        if size is not None:
            for path in files:
                try:
                    if path.stat().st_size == size:
                        return path
                except OSError:
                    continue
        return None

    async def diagnose_downloads_mount(self) -> MountDiagnosis:
        try:
            rows = await self._client.torrents_info(category=self._category)
        except Exception:  # noqa: BLE001 - a diagnostic must never raise
            return MountDiagnosis(supported=True)
        completed = [r for r in rows if r.progress >= 1.0 and r.content_path]
        if not completed:
            return MountDiagnosis(supported=True, completed_downloads=0, mount_has_files=True)
        sample = completed[: self._DIAGNOSIS_SAMPLE]
        resolvable = 0
        for info in sample:
            local = self._local_path(info)
            if await asyncio.to_thread(_path_has_file, local):
                resolvable += 1
        has_files = await asyncio.to_thread(self._mount_has_any_file)
        client_dir = completed[0].save_path or None
        return MountDiagnosis(
            supported=True,
            completed_downloads=len(completed),
            mount_has_files=has_files,
            resolvable_downloads=resolvable,
            sampled_downloads=len(sample),
            client_downloads_dir=client_dir,
        )

    async def downloads_mount_healthy(self) -> bool:
        """Whether DroppedNeedle's downloads MOUNT itself is usable (mirrors
        ``SabnzbdDownloadClient.downloads_mount_healthy`` - see its docstring for
        why only the mount root is checked, never the per-job folder)."""

        def _ok() -> bool:
            try:
                if not self._mount.is_dir():
                    return False
                next(self._mount.iterdir(), None)
                return True
            except OSError:
                return False

        return await asyncio.to_thread(_ok)

    # --- internals --------------------------------------------------------------

    def _materialization(
        self,
        *,
        state: str,
        mount_healthy: bool,
        remote_storage: str = "",
        workspace_path: str = "",
    ) -> DownloadMaterialization:
        """The ONLY construction point for this adapter's ``DownloadMaterialization``.

        There is deliberately no ``file_paths`` parameter. The acquisition cleanup
        journal unlinks every path reported in ``file_paths``, and a seeding
        torrent's bytes are never the attempt's to remove - the import COPIES them
        out and the torrent keeps seeding under its category. Leaving the field out
        of the signature means a caller cannot reintroduce it one return-path at a
        time; anyone who needs to must change this factory and read this note first.
        ``workspace_path`` still carries the location as cleanup evidence."""
        return DownloadMaterialization(
            state=state,
            remote_storage=remote_storage,
            mount_root=str(self._mount),
            workspace_path=workspace_path,
            mount_healthy=mount_healthy,
        )

    async def _remove_unless_seeding(self, handle: TaskHandle, *, action: str) -> bool:
        """Shared body of ``abort``/``discard_client_artifacts`` (mirrors slskd's
        ``_remove_transfer_records``): remove an incomplete torrent WITH its partial
        data, never touch a completed one, and treat "already gone" as success."""
        info = await self._find(handle)
        if info is None:
            return True
        if info.progress >= 1.0:
            logger.info(
                "qbittorrent: %s left completed torrent %s seeding (no delete)",
                action,
                info.hash,
            )
            return True
        return await self._client.delete_torrents(info.hash, delete_files=True)

    async def _recover(self, request: EnqueueRequest, correlation_id: str) -> QbtTorrentInfo | None:
        return await self._find(TaskHandle(source=SOURCE, job_name=correlation_id))

    @staticmethod
    def _handle(info: QbtTorrentInfo, correlation_id: str) -> TaskHandle:
        return TaskHandle(
            source=SOURCE,
            job_name=correlation_id,
        )

    async def _find(self, handle: TaskHandle) -> QbtTorrentInfo | None:
        if handle.job_name:
            rows = await self._client.torrents_info(tag=handle.job_name)
            owned = [
                row
                for row in rows
                if handle.job_name in {tag.strip() for tag in row.tags.split(",")}
                and row.category == self._category
            ]
            if len(owned) == 1:
                return owned[0]
        return None

    def _local_path(self, info: QbtTorrentInfo) -> Path:
        """Map the category save directory onto the configured downloads mount."""
        remote = PurePosixPath(info.content_path)
        if not info.save_path:
            raise ValueError("qBittorrent did not report its save path")
        rel = remote.relative_to(PurePosixPath(info.save_path))
        if not rel.parts or ".." in rel.parts:
            raise ValueError("Torrent path is outside its save directory")
        local = self._mount / Path(*rel.parts)
        if not local.resolve().is_relative_to(self._mount.resolve()):
            raise ValueError("Torrent path escapes the downloads mount")
        return local

    def _enumerate_audio(self, root_path: Path) -> list[Path]:
        """Audio files under the torrent's content path (bounded DFS), confined to
        the mount. A single-file torrent returns just that file."""
        mount = self._mount.resolve()
        try:
            root = root_path.resolve()
        except OSError:
            return []
        if not root.is_relative_to(mount):
            return []
        if root.is_file():
            return [root] if root.suffix.lower() in _AUDIO_SUFFIXES else []
        if not root.is_dir():
            return []
        out: list[Path] = []
        stack = [root]
        seen = 0
        while stack:
            try:
                entries = list(stack.pop().iterdir())
            except OSError:
                continue
            for entry in entries:
                seen += 1
                if seen > 10000:
                    return out
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    stack.append(entry)
                elif entry.is_file() and entry.suffix.lower() in _AUDIO_SUFFIXES:
                    out.append(entry)
        return out

    def _mount_has_any_file(self) -> bool:
        try:
            stack = [self._mount]
            seen = 0
            while stack:
                for entry in stack.pop().iterdir():
                    seen += 1
                    if seen > 5000:
                        return True
                    if entry.is_file():
                        return True
                    if entry.is_dir():
                        stack.append(entry)
        except OSError:
            return False
        return False


def _path_has_file(path: Path) -> bool:
    try:
        if path.is_file():
            return True
        return path.is_dir() and any(p.is_file() for p in path.iterdir())
    except OSError:
        return False


def _supports_api_key(version: str) -> bool:
    """qBittorrent added Bearer API-key authentication in 5.2.0."""
    match = re.search(r"(\d+)\.(\d+)", version or "")
    return bool(match and (int(match.group(1)), int(match.group(2))) >= (5, 2))


def _map_status(info: QbtTorrentInfo) -> DownloadTaskStatus:
    state = info.state.lower()
    bytes_total = info.size
    bytes_downloaded = min(info.downloaded, bytes_total) if bytes_total else info.downloaded
    percent = round(info.progress * 100.0, 1)
    if state in _FAILED_STATES:
        return DownloadTaskStatus(
            task_id="",
            status="failed",
            error="qBittorrent reported an error for this torrent",
            bytes_total=bytes_total,
            matched_transfers=1,
        )
    if (info.progress >= 1.0 or state in _SEEDING_STATES) and state not in _CHECKING_STATES:
        # Download done - seeding continues in qBittorrent but the lifecycle here is over.
        return DownloadTaskStatus(
            task_id="",
            status="completed",
            files_total=1,
            files_completed=1,
            bytes_total=bytes_total,
            bytes_downloaded=bytes_total,
            progress_percent=100.0,
            matched_transfers=1,
        )
    if state in _CHECKING_STATES:
        return DownloadTaskStatus(
            task_id="",
            status="processing",
            files_total=1,
            bytes_total=bytes_total,
            bytes_downloaded=bytes_downloaded,
            progress_percent=percent,
            matched_transfers=1,
        )
    if state in _ACTIVE_STATES:
        return DownloadTaskStatus(
            task_id="",
            status="downloading",
            files_total=1,
            bytes_total=bytes_total,
            bytes_downloaded=bytes_downloaded,
            progress_percent=percent,
            has_active_transfer=True,
            matched_transfers=1,
        )
    # metaDL / stalled / queued / paused / allocating / unknown: waiting, 0-byte states.
    return DownloadTaskStatus(
        task_id="",
        status="queued",
        files_total=1,
        bytes_total=bytes_total,
        bytes_downloaded=bytes_downloaded,
        progress_percent=percent,
        matched_transfers=1,
    )


class TorrentPlugin(QbittorrentDownloadClient):
    """Prowlarr or direct Torznab search, qBittorrent transfer, isolated import copies."""

    def __init__(self, context):
        self.ctx = context
        self._locks = {}
        self._validated = None
        # Last good status per job, replayed while qBittorrent is briefly unreachable.
        self._last_status = {}
        self._refresh()

    def _refresh(self):
        settings = self.ctx.settings
        url = settings.get("qbittorrent_url", "").strip().rstrip("/")
        key = _secret(settings.get("qbittorrent_api_key", ""))
        QbittorrentDownloadClient.__init__(
            self,
            QbittorrentClient(self.ctx.http, url, key),
            url,
            key,
            Path(settings.get("downloads_path", "") or "."),
            category=settings.get("category", "").strip() or "droppedneedle",
        )
        self._stage = Path(settings.get("staging_path", "") or ".")

    def _validate(self):
        settings = dict(self.ctx.settings)
        snapshot = tuple(sorted(settings.items()))
        # Polls call this for every task; only re-check paths when settings change.
        if snapshot == self._validated:
            return
        self._refresh()
        for key in ("qbittorrent_url", "qbittorrent_api_key", "downloads_path", "staging_path"):
            if not settings.get(key, "").strip():
                raise ValueError(f"Configure {key}")
        _http_url(self._url)
        if not self._mount.is_absolute() or not self._stage.is_absolute():
            raise ValueError("Downloads and staging paths must be absolute")
        mount, stage = self._mount.resolve(), self._stage.resolve()
        if stage.is_relative_to(mount) or mount.is_relative_to(stage):
            raise ValueError("Staging and downloads paths must be separate, non-nested directories")
        backend = settings.get("search_backend", "").strip() or "prowlarr"
        if backend not in ("prowlarr", "torznab"):
            raise ValueError("search_backend must be prowlarr or torznab")
        for suffix in ("url", "api_key"):
            if not settings.get(f"{backend}_{suffix}", "").strip():
                raise ValueError(f"Configure {backend}_{suffix}")
        _http_url(settings[f"{backend}_url"])
        self._categories()
        self._validated = snapshot

    def _categories(self):
        raw = self.ctx.settings.get("categories", "").strip() or "3000"
        values = [int(x.strip()) for x in raw.split(",")]
        if not values or any(x <= 0 for x in values):
            raise ValueError("Categories must be positive comma-separated IDs")
        return values

    @property
    def indexer_name(self):
        return SOURCE

    def is_configured(self):
        try:
            self._validate()
            return True
        except (ValueError, OSError):
            return False

    async def health_check(self):
        try:
            self._validate()
            status = await super().health_check()
            if status.status != "ok":
                return status
            if not await self.downloads_mount_healthy():
                return ServiceStatus(status="error", message="Downloads mount is not readable")
            await asyncio.to_thread(self._check_staging)
            if self.ctx.settings.get("search_backend", "prowlarr") == "torznab":
                await self._torznab("", 15.0, caps=True)
            else:
                await self._prowlarr().system_status()
            return ServiceStatus(
                status="ok",
                version=status.version,
                message="Torrent indexer, qBittorrent, and mounts are ready",
            )
        except Exception:  # noqa: BLE001 - health must never expose credentials
            return ServiceStatus(
                status="error",
                message="Check plugin settings, service connectivity, and mount permissions",
            )

    def _check_staging(self):
        self._stage.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=self._stage):
            pass

    def _prowlarr(self):
        return ProwlarrClient(
            self.ctx.http,
            self.ctx.settings["prowlarr_url"].strip(),
            _secret(self.ctx.settings["prowlarr_api_key"]),
        )

    async def search_album(
        self, artist_name, album_title, year=None, track_count=None, *, timeout=30.0
    ):
        return await self._search(artist_name, album_title, timeout)

    async def search_track(
        self, artist_name, track_title, album_title=None, duration_seconds=None, *, timeout=30.0
    ):
        return await self._search(artist_name, album_title or track_title, timeout)

    async def _search(self, artist, title, timeout):
        self._validate()
        async with asyncio.timeout(timeout):
            query = f"{artist} {title}".strip()
            backend = self.ctx.settings.get("search_backend", "prowlarr")
            secret = _secret(self.ctx.settings.get(f"{backend}_api_key", ""))
            if backend == "torznab":
                rows = await self._torznab(query, timeout)
            else:
                client = self._prowlarr()
                hits = await client.search(query, self._categories(), timeout=timeout)
                rows = [
                    {
                        "title": r.title,
                        "size": r.size,
                        "seeders": r.seeders,
                        "download_url": client.absolute_url(r.download_url)
                        if r.download_url
                        else "",
                        "magnet_url": r.magnet_url,
                        "info_hash": r.info_hash,
                        "categories": [c.id for c in r.categories],
                    }
                    for r in hits
                    if r.protocol.lower() == "torrent"
                ]
        results, seen = [], set()
        for row in rows[:100]:
            if row["seeders"] == 0 or 3020 in row.get("categories", []):
                continue
            download, magnet = row.get("download_url", ""), row.get("magnet_url", "")
            if magnet and not magnet.startswith("magnet:"):
                # Prowlarr (and Jackett) proxy magnets as HTTP links that redirect.
                download, magnet = download or magnet, ""
            if not download and not magnet:
                continue
            download = download.replace(secret, _KEY_PLACEHOLDER) if secret else download
            payload = json.dumps(
                {
                    "download_url": download,
                    "magnet_url": magnet,
                    "info_hash": row.get("info_hash", ""),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            identity = row.get("info_hash", "").lower() or magnet or download
            if identity in seen:
                continue
            seen.add(identity)
            score = PluginScoringHelper().album_match(artist, title, row["title"])
            # Unknown seeder counts require manual selection with the default host thresholds.
            if row["seeders"] is None:
                score = min(score, 0.69)
            results.append(
                IndexerResult(
                    source=SOURCE,
                    plugin=PluginSearchResult(
                        title=row["title"],
                        size_bytes=max(0, row["size"]),
                        score=score,
                        quality_tier=_quality(row["title"], row.get("categories", [])),
                        payload=payload,
                    ),
                )
            )
        return results

    async def _torznab(self, query, timeout, *, caps=False):
        from xml.etree import ElementTree as ET

        settings = self.ctx.settings
        url = settings["torznab_url"].strip().rstrip("/")
        if not url.endswith("/api"):
            url += "/api"
        params = {"apikey": _secret(settings["torznab_api_key"]), "t": "caps" if caps else "search"}
        if not caps:
            params.update(
                q=query, cat=",".join(map(str, self._categories())), extended="1", limit="100"
            )
        # Bound bytes before XML parsing, disable redirects and reject DTD/entities.
        async with self.ctx.http.stream(
            "GET", url, params=params, timeout=timeout, follow_redirects=False
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(f"Torznab HTTP {response.status_code}")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError("Torznab response exceeds 8 MiB")
        # Reject non-UTF-8 XML (including UTF-16 entity-declaration bypasses).
        xml = bytes(raw).decode("utf-8-sig")
        if "\x00" in xml or re.search(r"<!\s*(DOCTYPE|ENTITY)", xml, re.IGNORECASE):
            raise ValueError("Torznab XML contains a forbidden declaration")
        root = ET.fromstring(xml)
        if root.tag.rsplit("}", 1)[-1] == "error":
            raise RuntimeError("Torznab API rejected the request")
        if caps:
            return []
        rows = []
        for item in root.findall(".//item")[:100]:
            attrs = {}
            for child in item:
                if child.tag.rsplit("}", 1)[-1] == "attr":
                    attrs.setdefault(child.get("name", "").lower(), []).append(
                        child.get("value", "")
                    )

            def first(name, default="", attrs=attrs):
                return attrs.get(name, [default])[0]

            enclosure = item.find("enclosure")
            link = (
                (enclosure.get("url", "") if enclosure is not None else "")
                or item.findtext("link")
                or ""
            )
            magnet = first("magneturl") or (link if link.startswith("magnet:") else "")
            rows.append(
                {
                    "title": (item.findtext("title") or "").strip(),
                    "size": _number(
                        first("size") or (enclosure.get("length") if enclosure is not None else ""),
                        0,
                    ),
                    "seeders": _number(first("seeders")),
                    "magnet_url": magnet,
                    "download_url": urljoin(url, link)
                    if link and not link.startswith("magnet:")
                    else "",
                    "info_hash": first("infohash"),
                    "categories": [_number(c, 0) for c in attrs.get("category", [])],
                }
            )
        return rows

    async def enqueue(self, request):
        self._validate()
        payload = json.loads(request.payload)
        if not isinstance(payload, dict):
            raise TypeError("Invalid torrent payload")
        for key in ("download_url", "magnet_url"):
            value = payload.get(key, "")
            if not isinstance(value, str) or any(c in value for c in "\r\n"):
                raise ValueError("Invalid torrent URL")
            if value:
                if key == "download_url":
                    _http_url(value)
                elif not value.startswith("magnet:?"):
                    raise ValueError("Invalid magnet URL")
        tag = request.job_name or f"droppedneedle-{request.task_id}"
        if not re.fullmatch(r"droppedneedle-[A-Za-z0-9_-]+", tag):
            raise ValueError("Invalid task correlation tag")
        download = payload.get("download_url", "")
        if _KEY_PLACEHOLDER in download:
            payload["download_url"] = download.replace(
                _KEY_PLACEHOLDER, self._indexer_key(download)
            )
            request = msgspec.structs.replace(request, payload=json.dumps(payload))
        async with self._locks.setdefault("enqueue:" + tag, asyncio.Lock()):
            return await super().enqueue(request)

    def _indexer_key(self, url):
        """The configured key for the indexer that issued ``url``; never another host."""
        for backend in ("prowlarr", "torznab"):
            base = self.ctx.settings.get(f"{backend}_url", "").strip()
            key = self.ctx.settings.get(f"{backend}_api_key", "").strip()
            if base and key and _origin(base) == _origin(url):
                return _secret(key)
        raise ValueError("Grab link does not belong to a configured indexer")

    async def get_status(self, handle):
        self._validate()
        try:
            status = await super().get_status(handle)
        except QbittorrentApiError as exc:
            # The host only pauses for slskd outages; a raise here fails the task and
            # the cleanup journal then deletes the partial torrent. Report no active
            # transfer instead, so the host's queued timeout stays the backstop.
            if exc.auth:
                raise
            logger.warning("qbittorrent unavailable while polling %s: %s", handle.job_name, exc)
            last = self._last_status.get(handle.job_name)
            if last is None:
                return DownloadTaskStatus(task_id="", status="queued", matched_transfers=1)
            return msgspec.structs.replace(last, has_active_transfer=False)
        if status.status in ("queued", "downloading", "processing") and status.matched_transfers:
            self._last_status.pop(handle.job_name, None)
            self._last_status[handle.job_name] = status
            while len(self._last_status) > 2000:
                self._last_status.pop(next(iter(self._last_status)))
        else:
            self._last_status.pop(handle.job_name, None)
        return status

    async def abort(self, handle):
        self._validate()
        # Only category + exact task-tag owned torrents can be removed by the base client.
        return await super().abort(handle)

    def _job_dir(self, handle):
        if not handle.job_name:
            raise ValueError("Missing durable torrent correlation tag")
        name = hashlib.sha256(handle.job_name.encode()).hexdigest()
        root = self._stage.resolve()
        job = root / name
        if job.is_symlink():
            raise ValueError("Staging job cannot be a symlink")
        return job

    async def list_completed_files(self, handle):
        self._validate()
        job = self._job_dir(handle)
        lock = self._locks.setdefault(str(job), asyncio.Lock())
        async with lock:
            # Completed staging survives the torrent being removed or rechecked later.
            staged = await asyncio.to_thread(self._read_manifest, job)
            if staged is not None:
                return staged
            sources = await super().list_completed_files(handle)
            if not sources:
                return []
            task = asyncio.create_task(asyncio.to_thread(self._stage_files, job, sources))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Do not let cleanup race a still-running filesystem copy.
                await task
                raise

    @staticmethod
    def _read_manifest(job):
        marker = job / ".ready.json"
        if not marker.exists():
            return None
        candidates = [job / name for name in json.loads(marker.read_text())]
        if any(not p.resolve().is_relative_to(job.resolve()) or p.is_symlink() for p in candidates):
            raise ValueError("Invalid staging manifest path")
        return [p for p in candidates if p.is_file()]

    def _stage_files(self, job, sources):
        job.mkdir(parents=True, exist_ok=True)
        staged = self._read_manifest(job)
        if staged is not None:
            return staged
        mount = self._mount.resolve()
        names = []
        for source in sources:
            resolved = source.resolve()
            if source.is_symlink() or not resolved.is_relative_to(mount):
                raise ValueError("Torrent file escapes downloads mount")
            # Preserve relative paths (including disc directories and duplicate basenames).
            relative = resolved.relative_to(mount)
            target = job / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or not target.resolve().is_relative_to(job.resolve()):
                raise ValueError("Staging destination escapes job directory")
            fd, tmp = tempfile.mkstemp(prefix=".copy-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as output, resolved.open("rb") as source_file:
                    _copy_file(source_file, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(tmp, target)
            finally:
                Path(tmp).unlink(missing_ok=True)
            names.append(str(relative))
        tmp_marker = job / ".ready.tmp"
        with tmp_marker.open("w") as output:
            json.dump(names, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp_marker, job / ".ready.json")
        return [job / name for name in names]

    async def get_file_path(self, handle, remote_filename, size=None):
        files = await self.list_completed_files(handle)
        matches = [p for p in files if p.name == Path(remote_filename).name]
        return matches[0] if len(matches) == 1 else None

    async def inspect_materialization(self, handle):
        self._validate()
        evidence = await super().inspect_materialization(handle)
        job = self._job_dir(handle)
        # No file_paths: the v2.15 cleanup journal only unlinks fingerprinted
        # soulseek paths and parks any other source's paths in needs_attention.
        # Torrent bytes are never reported; discard_client_artifacts removes
        # the staging job directory itself.
        return DownloadMaterialization(
            state=evidence.state,
            mount_root=str(self._stage.resolve()),
            workspace_path=str(job),
            mount_healthy=evidence.mount_healthy,
        )

    async def discard_client_artifacts(self, handle):
        self._validate()
        result = await super().discard_client_artifacts(handle)
        if result:
            job = self._job_dir(handle)
            async with self._locks.setdefault(str(job), asyncio.Lock()):
                await asyncio.to_thread(shutil.rmtree, job, True)
        return result

    async def diagnose_downloads_mount(self):
        self._validate()
        return await super().diagnose_downloads_mount()


def _http_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Expected an HTTP(S) service URL without embedded credentials")


def _origin(url):
    parsed = urlsplit(url)
    default = {"http": 80, "https": 443}.get(parsed.scheme)
    return parsed.scheme, (parsed.hostname or "").lower(), parsed.port or default


def _copy_file(source, output):
    """Kernel-side copy (a reflink on btrfs/XFS), else a userspace copy."""
    copied = 0
    try:
        while chunk := os.copy_file_range(source.fileno(), output.fileno(), 1 << 30):
            copied += chunk
        return
    except (AttributeError, OSError) as exc:
        unsupported = {errno.EXDEV, errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EPERM}
        if copied or (isinstance(exc, OSError) and exc.errno not in unsupported):
            raise
    shutil.copyfileobj(source, output)


def _number(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _quality(title, categories):
    if re.search(r"\b(flac|alac|lossless)\b", title, re.IGNORECASE) or 3040 in categories:
        return "lossless"
    if 3020 in categories:
        return ""
    for bitrate in (320, 256, 192):
        if re.search(rf"\b{bitrate}\b", title):
            return f"mp3_{bitrate}"
    return ""


def _secret(value):
    """Accept the fork's Fernet ciphertext without writing plaintext settings.

    v2.15.0 masks secret fields but does not decrypt/encrypt plugin settings.
    The host initializes its crypto key before loading plugins.
    """
    value = value.strip()
    if value.startswith("gAAAA"):
        from infrastructure.crypto import decrypt

        plaintext, legacy = decrypt(value)
        if legacy:
            raise ValueError("Plugin credential cannot be decrypted with this installation key")
        return plaintext.strip()
    return value
