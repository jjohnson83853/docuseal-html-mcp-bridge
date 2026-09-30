"""ASGI application serving only opaque, format-bound downloads."""

from __future__ import annotations

import asyncio
import logging
import re
import time

from pathlib import Path
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse
from starlette.routing import Route

from document_export import ArtifactStore

logger = logging.getLogger(__name__)
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
MIME_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


async def _settle_task(task: asyncio.Task):
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            continue
    try:
        return task.result()
    except BaseException:
        return None


async def _store_call(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _settle_task(task)
        raise


async def _acquire_store(store: ArtifactStore, token: str, artifact_format: str):
    task = asyncio.create_task(
        asyncio.to_thread(store.acquire, token, artifact_format)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        acquired = await _settle_task(task)
        if acquired is not None:
            release_task = asyncio.create_task(
                asyncio.to_thread(store.release, acquired[2])
            )
            await _settle_task(release_task)
        raise


class _LeasedFileResponse(FileResponse):
    def __init__(self, path: Path, filename: str, artifact_format: str,
                 store: ArtifactStore, token: str, started: float) -> None:
        super().__init__(
            path,
            media_type=MIME_TYPES[artifact_format],
            filename=filename,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )
        self._store = store
        self._token = token
        self._artifact_format = artifact_format
        self._started = started

    async def __call__(self, scope, receive, send) -> None:
        status_code = 200

        async def capture_status(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await super().__call__(scope, receive, capture_status)
            logger.info("artifact_download_complete", extra={
                "operation": "download",
                "status_code": status_code,
                "format": self._artifact_format,
                "duration_ms": int((time.monotonic() - self._started) * 1000),
            })
        finally:
            await _store_call(self._store.release, self._token)

async def _download(request: Request):
    started = time.monotonic()
    token = request.query_params.get("token", "")
    artifact_format = request.query_params.get("format", "")
    if not TOKEN_RE.fullmatch(token) or artifact_format not in MIME_TYPES:
        logger.info("artifact_download_unavailable", extra={"operation": "download", "status_code": 404, "format": artifact_format if artifact_format in MIME_TYPES else "unsupported", "duration_ms": int((time.monotonic() - started) * 1000)})
        return PlainTextResponse("Not found", status_code=404)
    store: ArtifactStore = request.app.state.store
    acquired = await _acquire_store(store, token, artifact_format)
    if acquired is None:
        logger.info("artifact_download_unavailable", extra={"operation": "download", "status_code": 404, "format": artifact_format, "duration_ms": int((time.monotonic() - started) * 1000)})
        return PlainTextResponse("Not found", status_code=404)
    path, filename, lease_token = acquired

    return _LeasedFileResponse(
        path,
        filename,
        artifact_format,
        store,
        lease_token,
        started,
    )


def create_artifact_app(store: ArtifactStore) -> Starlette:
    app = Starlette(debug=False, routes=[Route("/download", _download, methods=["GET"])])
    app.state.store = store
    return app
