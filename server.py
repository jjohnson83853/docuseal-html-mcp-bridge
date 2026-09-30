"""LibreChat Streamable HTTP MCP bridge and separate artifact listener."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator, Mapping

from mcp.server.fastmcp import FastMCP
import uvicorn

from artifact_server import create_artifact_app
from docuseal_api import DocuSealClient
from document_export import ArtifactStore, RenderLimits, render_template_documents as render_documents


class _JsonLogFormatter(logging.Formatter):
    fields = ("operation", "correlation_id", "upstream_host", "path", "status_code",
              "duration_ms", "error_class", "format", "docuseal_api_key_configured")

    def format(self, record: logging.LogRecord) -> str:
        event = {"message": record.getMessage()}
        for field in self.fields:
            if hasattr(record, field):
                event[field] = getattr(record, field)
        return json.dumps(event, separators=(",", ":"))


logging.basicConfig(level=logging.INFO, format="%(message)s")
for _logger_name in ("httpx", "httpcore"):
    logging.getLogger(_logger_name).setLevel(logging.WARNING)

for _handler in logging.getLogger().handlers:
    _handler.setFormatter(_JsonLogFormatter())

logger = logging.getLogger(__name__)
DOCUSEAL_URL = os.getenv("DOCUSEAL_URL", "https://signature.bullprog.com").rstrip("/")
DOCUSEAL_API_KEY = os.getenv("DOCUSEAL_API_KEY", "")
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = 8000
DEFAULT_ARTIFACT_ROOT = Path(tempfile.gettempdir()) / "docuseal-artifacts"
SERVER_INSTRUCTIONS = """Render model-generated contract HTML to offline PDF and DOCX drafts for manual upload to DocuSeal. The exported files are not DocuSeal templates; users must manually place and verify fields in DocuSeal. Supported field elements are text-field, date-field, signature-field, initials-field, checkbox-field, radio-field, and select-field. Remaining tools query DocuSeal REST and need DOCUSEAL_API_KEY. Sending a submission may email signers and requires explicit user authorization."""


@dataclass(frozen=True)
class ServerConfig:
    artifact_port: int
    mcp_host: str
    artifact_host: str
    artifact_root: Path
    retention_seconds: int
    artifact_max_bytes: int
    artifact_max_files: int
    render_limits: RenderLimits


def _positive_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name, str(default))
    try:
        if isinstance(raw, bool):
            raise ValueError
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer") from None
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive finite number") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def load_config(environ: Mapping[str, str] | None = None) -> ServerConfig:
    values = os.environ if environ is None else environ
    raw_port = values.get("ARTIFACT_PORT", "8001")
    try:
        if not isinstance(raw_port, str) or not raw_port.isascii() or not raw_port.isdecimal():
            raise ValueError
        port = int(raw_port)
    except (TypeError, ValueError):
        raise ValueError("ARTIFACT_PORT must be an integer from 1024 to 65535, excluding 8000") from None
    if not 1024 <= port <= 65535 or port == MCP_PORT:
        raise ValueError("ARTIFACT_PORT must be from 1024 to 65535 and different from 8000")
    concurrent = _positive_int(values, "MAX_CONCURRENT_RENDERS", 1)
    if concurrent != 1:
        raise ValueError("MAX_CONCURRENT_RENDERS must be 1")
    retention = _positive_int(values, "ARTIFACT_RETENTION_SECONDS", 86400)
    if retention > 86400:
        raise ValueError("ARTIFACT_RETENTION_SECONDS cannot exceed 86400")
    limits = RenderLimits(
        max_input_bytes=_positive_int(values, "MAX_INPUT_BYTES", 2 * 1024 * 1024),
        max_output_bytes=_positive_int(values, "MAX_OUTPUT_BYTES", 20 * 1024 * 1024),
        max_pages=_positive_int(values, "MAX_PAGES", 100),
        max_image_bytes=_positive_int(values, "MAX_IMAGE_BYTES", 2 * 1024 * 1024),
        max_image_pixels=_positive_int(values, "MAX_IMAGE_PIXELS", 20_000_000),
        worker_timeout_seconds=_positive_float(values, "WORKER_TIMEOUT_SECONDS", 60),
    )
    artifact_root_value = values.get("ARTIFACT_DIR", str(DEFAULT_ARTIFACT_ROOT))
    if not artifact_root_value:
        raise ValueError("ARTIFACT_DIR must identify a dedicated artifact directory")
    artifact_root = Path(artifact_root_value).resolve()
    if artifact_root in {Path("/"), Path.cwd()}:
        raise ValueError("ARTIFACT_DIR must not be a filesystem root or the working directory")
    return ServerConfig(
        artifact_port=port,
        mcp_host=values.get("MCP_HOST", "127.0.0.1"),
        artifact_host=values.get("ARTIFACT_HOST", values.get("MCP_HOST", "127.0.0.1")),
        artifact_root=artifact_root,
        retention_seconds=retention,
        artifact_max_bytes=_positive_int(values, "MAX_ARTIFACT_BYTES", 256 * 1024 * 1024),
        artifact_max_files=_positive_int(values, "MAX_ARTIFACT_FILES", 100),
        render_limits=limits,
    )


mcp = FastMCP("DocuSeal REST bridge", instructions=SERVER_INSTRUCTIONS, host=MCP_HOST,
              port=MCP_PORT, streamable_http_path="/mcp")
_api_client = DocuSealClient(DOCUSEAL_URL, DOCUSEAL_API_KEY)
_artifact_store: ArtifactStore | None = None
_render_limits = RenderLimits()


@mcp.tool()
async def render_template_documents(template_name: str, html_content: str,
                                    document_size: str = "Letter") -> dict[str, str]:
    """Render upload-ready PDF and DOCX drafts for manual field placement in DocuSeal."""
    store = _artifact_store
    if store is None:
        raise RuntimeError("Artifact store is not initialized")
    return await render_documents(template_name, html_content, document_size,
                                  store=store,
                                  public_base_url=os.getenv("ARTIFACT_PUBLIC_BASE_URL", ""),
                                  limits=_render_limits)


@mcp.tool()
async def search_templates(query: str = "", limit: int = 10) -> dict:
    """Search templates by name and return DocuSeal template records including fields."""
    return await _api_client.search_templates(query, limit)


@mcp.tool()
async def get_template(template_id: int) -> dict:
    """Retrieve one DocuSeal template by its numeric template ID, including its field schema."""
    return await _api_client.get_template(template_id)


@mcp.tool()
async def list_submissions(template_id: int | None = None, status: str | None = None,
                           query: str | None = None, limit: int = 10) -> dict:
    """List submissions, optionally filtered by template ID, status, and search query."""
    return await _api_client.list_submissions(template_id, status, query, limit)


@mcp.tool()
async def send_submission(template_id: int, signer_email: str, signer_name: str,
                          signer_role: str = "Signer",
                          field_values: dict[str, Any] | None = None) -> dict:
    """Send a signing request for a template to one signer; this may send a real email."""
    return await _api_client.send_submission(template_id, signer_email, signer_name,
                                             signer_role, field_values)


@mcp.tool()
async def get_submission_status(submission_id: int) -> dict:
    """Retrieve a submission by its numeric ID and return DocuSeal's current status."""
    return await _api_client.get_submission_status(submission_id)


async def _cleanup_artifacts(store: ArtifactStore) -> None:
    while True:
        await asyncio.sleep(60)
        deleted = await asyncio.to_thread(store.cleanup_expired)
        if deleted:
            logger.info("artifacts_expired", extra={"operation": "cleanup", "status_code": 200,
                                                    "duration_ms": 0})


class _SecondaryUvicornServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def run_servers(config: ServerConfig) -> None:
    global _artifact_store, _render_limits
    _artifact_store = ArtifactStore(config.artifact_root,
                                    retention_seconds=config.retention_seconds,
                                    max_bytes=config.artifact_max_bytes,
                                    max_files=config.artifact_max_files)
    _render_limits = config.render_limits
    mcp_server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), host=config.mcp_host,
                                                port=MCP_PORT, access_log=False))
    artifact_server = _SecondaryUvicornServer(
        uvicorn.Config(create_artifact_app(_artifact_store), host=config.artifact_host,
                       port=config.artifact_port, access_log=False)
    )
    servers = (mcp_server, artifact_server)
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    cleanup_task = asyncio.create_task(_cleanup_artifacts(_artifact_store))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exception = task.exception()
            if exception is not None:
                raise exception
    finally:
        cleanup_task.cancel()
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(cleanup_task, return_exceptions=True)


def main() -> None:
    config = load_config()
    logger.info("bridge_starting", extra={"operation": "startup",
                                         "docuseal_api_key_configured": bool(os.getenv("DOCUSEAL_API_KEY"))})
    asyncio.run(run_servers(config))


if __name__ == "__main__":
    main()
