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
from docuseal_mcp import DEFAULT_DOCUSEAL_URL, DocuSealMcpClient
from document_export import ArtifactStore, RenderLimits, render_template_documents as render_documents


class _JsonLogFormatter(logging.Formatter):
    fields = ("operation", "correlation_id", "upstream_host", "path", "status_code",
              "duration_ms", "error_class", "format")

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
DOCUSEAL_URL = os.getenv("DOCUSEAL_URL", DEFAULT_DOCUSEAL_URL)
DOCUSEAL_MCP_BEARER_TOKEN = os.getenv("DOCUSEAL_MCP_BEARER_TOKEN", "")
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = 8000
DEFAULT_ARTIFACT_ROOT = Path(tempfile.gettempdir()) / "docuseal-artifacts"
SERVER_INSTRUCTIONS = """Render model-generated contract HTML to offline PDF and DOCX drafts for manual upload to DocuSeal. The exported files are not DocuSeal templates; users must manually place and verify fields in DocuSeal. Supported field elements are text-field, date-field, signature-field, initials-field, checkbox-field, radio-field, and select-field. DocuSeal operations use its native MCP tools with server-side Bearer credentials. search_documents supports free-text search only; submission status lookup and template/status filters are unavailable. Sending a submission may email signers and requires explicit user authorization. Author HTML for the draft with inline styles: JavaScript, SVG, iframes, external resources, and web fonts are unavailable and silently removed. Force page breaks with <div style="page-break-after: always"></div>. Use the document_html_guide prompt before authoring anything nontrivial."""

MAX_PROMPT_TOPIC_CHARS = 4000

DOCUMENT_HTML_GUIDE = """# HTML authoring guide for render_template_documents

You write one HTML fragment per document. It is rendered to PDF (WeasyPrint) and DOCX (html-for-docx) with the page size set by the `document_size` argument (Letter, Legal, A4, A3, A5). Follow this guide so both outputs look as expected.

## 1. Use inline styles for anything that must look right in both formats

The DOCX converter reads inline `style` attributes only; it ignores `<style>` blocks entirely. `<style>` blocks are rendered by the PDF path only.

- Put every style that affects layout or text appearance on the element as an inline `style` attribute.
- A `<style>` block may add print polish that appears in the PDF (for example `@page` margins or `widows`/`orphans`), but never rely on it for content structure.

## 2. Page breaks (works in BOTH formats)

Insert an explicit page break anywhere with:

    <div style="page-break-after: always"></div>

This is the only page-break mechanism the DOCX converter understands, and the PDF renderer honors it too. Never insert blank paragraphs to push content to the next page.

## 3. Keeping paragraphs and sections together

- PDF: wrap a block that must not be split across pages in `<div style="break-inside: avoid">...</div>`; `orphans` and `widows` are also honored.
- DOCX: there is no keep-together support. Keep a logical block short (a heading plus a few lines), put it before an explicit page break when it matters, or accept the split. Long paragraphs are always splittable in DOCX.

Rule of thumb: if a section must stay on one page in both formats, keep it short and place page breaks around it explicitly.

## 4. What is NOT available (silently removed, no error)

- JavaScript: all `<script>` elements are stripped. Nothing is interactive or computed.
- SVG, `<canvas>`, `<video>`, `<audio>`, `<iframe>`, `<object>`, `<embed>`: unsupported; removed.
- External resources: external stylesheets (`<link>`), `url()` and `@import` in CSS, external images, and remote fonts are all refused. There are no web fonts; only fonts installed in the renderer are available (`serif`, `sans-serif`, `monospace` map to Times New Roman, Arial, and Courier New in DOCX).
- `<meta>`, `<base>`: ignored.
- `on*` event attributes: stripped.
- Unknown HTML tags: the tag is dropped but its text content is kept, so stray tags cannot lose content.

## 5. Allowed HTML

Tags: `p, div, span, br, hr, h1-h6, strong, b, em, i, u, s, ul, ol, li, table, thead, tbody, tr, th, td, blockquote, pre, code, img, section, article`.
Attributes: `style, class, colspan, rowspan, width, height, alt, src`.

## 6. Layout

- Use tables for multi-column layouts; both formats support them. Flexbox and grid are not supported by the DOCX converter.
- Headings `h1`-`h6` map to heading styles; use them for structure instead of large bold paragraphs.
- Prefer explicit `margin` in px/pt/em. Absolute positioning does not survive DOCX conversion; avoid it.

## 7. Images

Only embedded data URIs work: `<img src="data:image/png;base64,...">` (also jpeg, gif, webp). Maximum 2 MiB encoded and 20 megapixels. Everything else (http(s)://, file://, relative paths) is dropped.

## 8. DocuSeal field placeholders

These seven elements render as visible labels `[Name - Type field]` in the drafts (the real interactive fields are placed later in DocuSeal):

    <text-field name="Legal name" role="Signer"></text-field>
    <date-field name="Effective date"></date-field>
    <signature-field name="Signature"></signature-field>
    <initials-field name="Initials"></initials-field>
    <checkbox-field name="Agreed"></checkbox-field>
    <radio-field name="Plan"></radio-field>
    <select-field name="Term"></select-field>

Rules: every field requires a non-empty `name`; `role` is optional. Styles on fields are restricted to spacing (max 100px), borders, display, font-weight/font-style, and text-align, and any style that could hide the label (opacity, visibility, transparent color, font-size 0) is removed.

## 9. Limits

- Input up to 2 MiB; output up to 100 pages by default (server-configured). Exceeding either fails the render and returns no files.
- One render runs at a time; a concurrent call is rejected immediately.
- The returned links are expiring download URLs for a finished PDF and DOCX; they are drafts for the user to upload to DocuSeal manually.
"""


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
    server_defined_rendering: bool = False


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
    DocuSealMcpClient._validated_url(
        values.get("DOCUSEAL_URL", DEFAULT_DOCUSEAL_URL)
    )
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
    raw_rendering_flag = values.get("DOCUSEAL_SERVER_DEFINED_RENDERING", "").strip().lower()
    if raw_rendering_flag not in {"", "0", "1", "true", "false", "yes", "no", "on", "off"}:
        raise ValueError("DOCUSEAL_SERVER_DEFINED_RENDERING must be a boolean-like value")
    server_defined_rendering = raw_rendering_flag in {"1", "true", "yes", "on"}
    return ServerConfig(
        artifact_port=port,
        mcp_host=values.get("MCP_HOST", "127.0.0.1"),
        artifact_host=values.get("ARTIFACT_HOST", values.get("MCP_HOST", "127.0.0.1")),
        artifact_root=artifact_root,
        retention_seconds=retention,
        artifact_max_bytes=_positive_int(values, "MAX_ARTIFACT_BYTES", 256 * 1024 * 1024),
        artifact_max_files=_positive_int(values, "MAX_ARTIFACT_FILES", 100),
        render_limits=limits,
        server_defined_rendering=server_defined_rendering,
    )


mcp = FastMCP("DocuSeal native MCP bridge", instructions=SERVER_INSTRUCTIONS, host=MCP_HOST,
              port=MCP_PORT, streamable_http_path="/mcp")
_mcp_client = DocuSealMcpClient(DOCUSEAL_URL, DOCUSEAL_MCP_BEARER_TOKEN)
_artifact_store: ArtifactStore | None = None
_render_limits = RenderLimits()
_server_defined_rendering = False


@mcp.prompt()
def document_html_guide() -> str:
    """Full HTML authoring guide: supported tags/CSS, page breaks, and unavailable technologies."""
    return DOCUMENT_HTML_GUIDE


@mcp.prompt()
def author_document_prompt(document_description: str = "A contract between two parties") -> str:
    """Build an authoring prompt that applies the guide to one document."""
    topic = str(document_description).strip()[:MAX_PROMPT_TOPIC_CHARS] or "A contract between two parties"
    return (
        "Write a complete, self-contained HTML document for the description below, "
        "then call render_template_documents with it.\n\n"
        "Hard requirements:\n"
        "- Author every visual style as an inline style attribute; the DOCX converter ignores <style> blocks.\n"
        "- Force page breaks only with <div style=\"page-break-after: always\"></div>.\n"
        "- Do not use JavaScript, SVG, flexbox, grid, external images, external fonts, or any external resources; they are silently removed.\n"
        "- Use tables for multi-column layout and h1-h6 for headings.\n"
        "- If any DocuSeal fields (text-field, date-field, signature-field, initials-field, checkbox-field, radio-field, select-field) are relevant, include them with non-empty name attributes where they should appear.\n"
        "- Keep sections short enough to fit a page, or surround them with explicit page breaks, so paragraphs and headings are not split awkwardly in DOCX.\n"
        "- Return only the HTML; do not add explanations.\n\n"
        f"Document: {topic}\n"
    )


@mcp.prompt()
def preflight_document_check(html_document: str) -> str:
    """Checklist prompt to validate HTML before calling render_template_documents."""
    document = str(html_document).strip()[:MAX_PROMPT_TOPIC_CHARS]
    return (
        "Check the HTML below against this checklist and fix every violation before "
        "calling render_template_documents. Return the corrected HTML only.\n\n"
        "1. No <script>, <style>-dependent layout, SVG, iframe, or external URLs (href/src/ url()/@import).\n"
        "2. Images, if any, are data:image/{png,jpeg,gif,webp} URIs under 2 MiB.\n"
        "3. All layout-affecting CSS is inline; <style> blocks, if present, only add PDF polish.\n"
        "4. Page breaks use <div style=\"page-break-after: always\"></div>, not blank paragraphs.\n"
        "5. Multi-column layout uses tables, not flexbox/grid.\n"
        "6. Every DocuSeal field element has a non-empty name attribute.\n"
        "7. No styles attempt to hide or erase field labels (opacity, visibility, transparent color, font-size 0).\n"
        "8. Headings use h1-h6; content is split into short blocks that fit one page.\n\n"
        f"HTML to check:\n{document}\n"
    )


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
                                  limits=_render_limits,
                                  server_defined=_server_defined_rendering)


@mcp.tool()
async def search_templates(query: str = "", limit: int = 10) -> dict:
    """Search templates by name using DocuSeal's native MCP tool."""
    return await _mcp_client.search_templates(query, limit)


@mcp.tool()
async def get_template(template_id: int) -> dict:
    """Load one DocuSeal template using the native MCP operation."""
    return await _mcp_client.load_template(template_id)


@mcp.tool()

async def search_documents(q: str = "", limit: int = 10) -> dict:
    """Search DocuSeal documents by free text; template and status filters are unavailable."""
    return await _mcp_client.search_documents(q, limit)


@mcp.tool()
async def send_submission(template_id: int, signer_email: str, signer_name: str,
                          signer_role: str = "Signer",
                          field_values: dict[str, Any] | None = None) -> dict:
    """Send a signing request for a template to one signer; this may send a real email."""
    return await _mcp_client.send_documents(
        template_id, signer_email, signer_name, signer_role, field_values
    )


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
    global _artifact_store, _render_limits, _server_defined_rendering
    _artifact_store = ArtifactStore(config.artifact_root,
                                    retention_seconds=config.retention_seconds,
                                    max_bytes=config.artifact_max_bytes,
                                    max_files=config.artifact_max_files)
    _render_limits = config.render_limits
    _server_defined_rendering = config.server_defined_rendering
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
    logger.info("bridge_starting", extra={"operation": "startup"})
    asyncio.run(run_servers(config))



if __name__ == "__main__":
    main()
