"""Offline HTML rendering and short-lived artifact publication."""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import io
import json
import logging
import math
import os
import signal
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import uuid
import warnings
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)
MAX_RETENTION_SECONDS = 24 * 60 * 60
MAX_WORKER_CPU_SECONDS = 45
MAX_WORKER_MEMORY_BYTES = 1024 * 1024 * 1024
MAX_WORKER_FILE_BYTES = 20 * 1024 * 1024
MAX_DOCX_PAGE_CHECK_SECONDS = 20
FIELD_TAGS = {
    "text-field": "Text", "date-field": "Date", "signature-field": "Signature",
    "initials-field": "Initials", "checkbox-field": "Checkbox",
    "radio-field": "Radio", "select-field": "Select",
}
ALLOWED_TAGS = {
    "html", "head", "body", "title", "p", "div", "span", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "em", "i",
    "u", "s", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th",
    "td", "blockquote", "pre", "code", "img", "section", "article",
}
ALLOWED_ATTRIBUTES = {"style", "class", "colspan", "rowspan", "width", "height", "alt", "src"}
CSS_URL = re.compile(r"(?:url\s*\(|@import)", re.IGNORECASE)
IMAGE_URI = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,([A-Za-z0-9+/=]+)$", re.I)
MAX_FIELD_SPACING_PX = 100
MAX_STYLE_CSS_BYTES = 100_000
FIELD_SPACING_UNITS = {
    "px": 1,
    "pt": 96 / 72,
    "pc": 16,
    "in": 96,
    "cm": 96 / 2.54,
    "mm": 96 / 25.4,
    "em": 16,
    "rem": 16,
    "ex": 8,
    "ch": 8,
}
FIELD_SPACING_VALUE = re.compile(
    r"(?P<magnitude>[0-9]+(?:\.[0-9]+)?)(?P<unit>px|pt|pc|in|cm|mm|em|rem|ex|ch)?"
)


def _safe_field_spacing(value: str, allow_auto: bool) -> bool:
    if allow_auto and value == "auto":
        return True
    match = FIELD_SPACING_VALUE.fullmatch(value)
    if match is None:
        return False
    magnitude = float(match.group("magnitude"))
    unit = match.group("unit")
    if magnitude and unit is None:
        return False
    return magnitude * FIELD_SPACING_UNITS.get(unit or "px", 1) <= MAX_FIELD_SPACING_PX


def _safe_style(value: str | None) -> str | None:
    style = value or ""
    if CSS_URL.search(style) or "\\" in style or "/*" in style or "*/" in style:
        return None
    return style


CSS_VECTOR = re.compile(
    r"(?:url\s*\(|@import|expression\s*\(|behavior\s*:|-moz-binding|</?style)", re.IGNORECASE
)


def _safe_document_style(value: str | None) -> str | None:
    style = _safe_style(value)
    if style is None or CSS_VECTOR.search(style) or len(style) > MAX_STYLE_CSS_BYTES:
        return None
    return style


def _safe_document_css(data: str) -> bool:
    if CSS_VECTOR.search(data) or "\\" in data or "/*" in data or "*/" in data:
        return False
    return len(data) <= MAX_STYLE_CSS_BYTES


def _safe_field_style(value: str | None) -> str | None:
    style = _safe_style(value)
    if style is None:
        return None
    declarations = []
    spacing_properties = {
        "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
        "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    }
    border_properties = {
        "border", "border-top", "border-right", "border-bottom", "border-left",
        "border-width", "border-style", "border-color", "border-radius", "box-sizing",
    }
    for declaration in style.split(";"):
        name, separator, declaration_value = declaration.partition(":")
        if not separator:
            continue
        name = name.strip().lower()
        normalized_value = re.sub(
            r"\s*!important\s*$", "", declaration_value.strip(), flags=re.IGNORECASE
        ).strip().lower()
        if name in spacing_properties:
            values = normalized_value.split()
            if len(values) > 4 or not values or any(
                not _safe_field_spacing(item, allow_auto=name.startswith("margin"))
                for item in values
            ):
                continue
        elif name == "display":
            if normalized_value not in {"block", "inline", "inline-block"}:
                continue
        elif name == "font-weight":
            if normalized_value not in {"normal", "bold", "bolder", "lighter"} | {
                str(weight) for weight in range(100, 1000, 100)
            }:
                continue
        elif name == "font-style":
            if normalized_value not in {"normal", "italic", "oblique"}:
                continue
        elif name == "text-align":
            if normalized_value not in {"left", "right", "center", "justify"}:
                continue
        elif name not in border_properties:
            continue
        declarations.append(declaration.strip())
    return "; ".join(declarations) or None


@dataclass(frozen=True)
class RenderLimits:
    max_input_bytes: int = 2 * 1024 * 1024
    max_output_bytes: int = 20 * 1024 * 1024
    max_pages: int = 100
    max_image_bytes: int = 2 * 1024 * 1024
    max_image_pixels: int = 20_000_000
    worker_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be positive and finite")


class DocumentRenderError(RuntimeError):
    """Safe rendering failure without renderer details."""


class ArtifactCapacityError(RuntimeError):
    """The configured artifact storage budget is exhausted."""


@dataclass(frozen=True)
class _Artifact:
    path: Path
    format: str
    filename: str
    expires_at: datetime


class ArtifactStore:
    def __init__(self, root: str | Path, clock: Callable[[], datetime] | None = None,
                 retention_seconds: int = MAX_RETENTION_SECONDS,
                 max_bytes: int = 256 * 1024 * 1024, max_files: int = 100) -> None:
        if (isinstance(retention_seconds, bool) or not isinstance(retention_seconds, int)
                or not 0 < retention_seconds <= MAX_RETENTION_SECONDS):
            raise ValueError("retention_seconds must be within 24 hours")
        if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0
                or isinstance(max_files, bool) or not isinstance(max_files, int) or max_files <= 0):
            raise ValueError("artifact budgets must be positive integers")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = self.root / ".docuseal-artifact-store"
        if marker.exists():
            if not marker.is_file() or marker.read_text(encoding="utf-8") != "docuseal-html-export-v1\n":
                raise ValueError("artifact directory marker is invalid")
        else:
            if any(self.root.iterdir()):
                raise ValueError("artifact directory must be dedicated to this service")
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as marker_file:
                marker_file.write("docuseal-html-export-v1\n")
                marker_file.flush()
                os.fsync(marker_file.fileno())
        os.chmod(self.root, 0o700)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.retention_seconds = retention_seconds
        self.max_bytes = max_bytes
        self.max_files = max_files
        self._lock = threading.RLock()
        self._tokens: dict[str, _Artifact] = {}
        self._active: dict[Path, int] = {}
        self._load_existing()
    def _load_existing(self) -> None:
        """Remove only this store's staged and UUID-named artifacts on restart."""
        artifact_name = re.compile(r"^[0-9a-f]{32}$")
        for child in self.root.iterdir():
            if child.name == ".docuseal-artifact-store":
                continue
            if not (child.name.startswith(".stage-") or artifact_name.fullmatch(child.name)):
                continue
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except OSError:
                logger.warning("artifact_cleanup_failed",
                               extra={"operation": "cleanup", "error_class": "OSError"})
    @property
    def artifact_count(self) -> int:
        with self._lock:
            return sum(1 for item in self._tokens.values() if item.path.exists())

    def publish_pair(self, template_name: str, pdf_bytes: bytes, docx_bytes: bytes) -> dict[str, str]:
        if not isinstance(pdf_bytes, bytes) or not isinstance(docx_bytes, bytes):
            raise ValueError("renderers must return bytes")
        now = self.clock()
        expires = now + timedelta(seconds=self.retention_seconds)
        clean_name = _safe_stem(template_name)
        pdf_token, docx_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        pair_id = uuid.uuid4().hex
        with self._lock:
            self.cleanup_expired()
            files, used = self._usage()
            incoming_bytes = len(pdf_bytes) + len(docx_bytes)
            if files + 2 > self.max_files or used + incoming_bytes > self.max_bytes:
                raise ArtifactCapacityError("Artifact storage capacity is unavailable")
            staged = Path(tempfile.mkdtemp(prefix=".stage-", dir=self.root))
            os.chmod(staged, 0o700)
            pdf_path, docx_path = staged / "document.pdf", staged / "document.docx"
            try:
                _write_private(pdf_path, pdf_bytes)
                _write_private(docx_path, docx_bytes)
                final = self.root / pair_id
                os.replace(staged, final)
                self._tokens[pdf_token] = _Artifact(final / "document.pdf", "pdf", clean_name + ".pdf", expires)
                self._tokens[docx_token] = _Artifact(final / "document.docx", "docx", clean_name + ".docx", expires)
            except BaseException:
                shutil.rmtree(staged, ignore_errors=True)
                raise
        logger.info("artifact_pair_published", extra={"operation": "artifact_publish", "status_code": 201})
        expires_iso = expires.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "pdf_url": f"/download?token={pdf_token}&format=pdf",
            "docx_url": f"/download?token={docx_token}&format=docx",
            "expires_at": expires_iso,
        }

    def discard_pair(self, links: dict[str, str]) -> None:
        expected = (("pdf_url", "pdf"), ("docx_url", "docx"))
        tokens = []
        for key, artifact_format in expected:
            try:
                query = parse_qs(urlsplit(links[key]).query)
                token, returned_format = query["token"], query["format"]
            except (KeyError, TypeError, ValueError):
                return
            if (len(token) != 1 or len(returned_format) != 1
                    or returned_format[0] != artifact_format
                    or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token[0])):
                return
            tokens.append(token[0])
        if tokens[0] == tokens[1]:
            return

        with self._lock:
            items = [self._tokens.get(token) for token in tokens]
            if (any(item is None for item in items)
                    or items[0].format != "pdf" or items[1].format != "docx"
                    or items[0].path.parent != items[1].path.parent):
                return
            pair = items[0].path.parent
            now = self.clock()
            if any(path.parent == pair for path in self._active):
                for token, item in zip(tokens, items):
                    self._tokens[token] = replace(item, expires_at=now)
                return
            try:
                shutil.rmtree(pair)
            except FileNotFoundError:
                pass
            except OSError:
                for token, item in zip(tokens, items):
                    self._tokens[token] = replace(item, expires_at=now)
                logger.warning(
                    "artifact_cleanup_failed",
                    extra={"operation": "cleanup", "error_class": "OSError"},
                )
                return
            for token in tokens:
                self._tokens.pop(token, None)


    def _usage(self) -> tuple[int, int]:
        paths = tuple(
            path for path in self.root.rglob("*")
            if path.is_file() and path.name != ".docuseal-artifact-store"
        )
        return len(paths), sum(path.stat().st_size for path in paths)

    def acquire(self, token: str, requested_format: str) -> tuple[Path, str, str] | None:
        if requested_format not in ("pdf", "docx"):
            return None
        with self._lock:
            item = self._tokens.get(token)
            if item is None or item.format != requested_format or self.clock() >= item.expires_at:
                return None
            if not item.path.is_file():
                return None
            self._active[item.path] = self._active.get(item.path, 0) + 1
            return item.path, item.filename, token
    def release(self, token: str) -> None:
        with self._lock:
            item = self._tokens.get(token)
            if item is None:
                return
            count = self._active.get(item.path, 0) - 1
            if count > 0:
                self._active[item.path] = count
            else:
                self._active.pop(item.path, None)

    def cleanup_expired(self) -> int:
        deleted = 0
        with self._lock:
            active_pairs = {path.parent for path in self._active}
            expired_pairs: dict[Path, list[str]] = {}
            now = self.clock()
            for token, item in self._tokens.items():
                if now >= item.expires_at:
                    expired_pairs.setdefault(item.path.parent, []).append(token)
            for pair, tokens in expired_pairs.items():
                if pair in active_pairs:
                    continue
                try:
                    shutil.rmtree(pair)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning(
                        "artifact_cleanup_failed",
                        extra={"operation": "cleanup", "error_class": "OSError"},
                    )
                    continue
                for token in tokens:
                    self._tokens.pop(token, None)
                    deleted += 1
        return deleted


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


def _safe_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", str(name)[:256].strip()).strip("-_").lower()[:64]
    return stem or "document"


class _Normalizer(HTMLParser):
    def __init__(self, limits: RenderLimits, server_defined: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.limits = limits
        self.server_defined = server_defined
        self.in_style = False
        self.output: list[str] = []
        self.skip: list[str] = []
        self.open_fields: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.in_style:
            return
        tag = tag.lower()
        if tag in {"script", "style", "svg", "iframe", "object", "embed", "link", "meta", "base"}:
            if tag == "style" and not self.server_defined:
                self.in_style = True
                self.output.append("<style>")
                return
            if tag in {"script", "style", "svg", "iframe", "object"}:
                self.skip.append(tag)
            return
        if tag in FIELD_TAGS:
            name = (dict(attrs)).get("name")
            field_type = FIELD_TAGS[tag]
            if not name or not name.strip():
                raise ValueError("Recognized field requires a name")
            label = f"[{name.strip()} — {field_type} field]"
            style = _safe_field_style((dict(attrs)).get("style"))
            style_attribute = (
                "" if style is None
                else f' style="{html.escape(style, quote=True)}"'
            )
            self.output.extend((
                f'<span class="docuseal-field"{style_attribute}>',
                html.escape(label),
            ))
            self.open_fields.append(tag)
            return
        if tag not in ALLOWED_TAGS:
            return
        safe_attrs = []
        for key, value in attrs:
            key = key.lower()
            value = value or ""
            if key not in ALLOWED_ATTRIBUTES or key.startswith("on"):
                continue
            if key == "style":
                safe_style = (
                    _safe_field_style(value) if self.server_defined
                    else _safe_document_style(value)
                )
                if safe_style is None:
                    continue
                value = safe_style
            if key == "src":
                if not IMAGE_URI.fullmatch(value):
                    continue
                _decode_image(value, self.limits)
            safe_attrs.append(f' {key}="{html.escape(value, quote=True)}"')
        self.output.append("<" + tag + "".join(safe_attrs) + ">")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skip or self.in_style:
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)
    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.in_style:
            if tag == "style":
                self.in_style = False
                self.output.append("</style>")
            return
        if tag in self.skip:
            self.skip.remove(tag)
            return
        if self.skip:
            return
        if tag in FIELD_TAGS:
            if not self.open_fields or self.open_fields[-1] != tag:
                raise ValueError("Malformed DocuSeal field markup")
            self.open_fields.pop()
            self.output.append("</span>")
            return
        if tag in ALLOWED_TAGS and tag not in {"img", "br", "hr"}:
            self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.in_style:
            if _safe_document_css(data):
                self.output.append(data)
            return
        if not self.skip:
            self.output.append(html.escape(data))


def _decode_image(uri: str, limits: RenderLimits) -> bytes | None:
    match = IMAGE_URI.fullmatch(uri)
    if not match:
        return None
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise DocumentRenderError("Embedded image data is invalid") from None
    if len(raw) > limits.max_image_bytes:
        raise DocumentRenderError("Embedded image exceeds configured byte limit")
    try:
        from PIL import Image
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > limits.max_image_pixels:
                    raise DocumentRenderError("Embedded image exceeds configured pixel limit")
                image.verify()
    except ImportError:
        raise DocumentRenderError("Image validation support is unavailable") from None
    except DocumentRenderError:
        raise
    except Exception:
        raise DocumentRenderError("Embedded image is invalid") from None
    return raw


def normalize_html(source: str, limits: RenderLimits,
                   server_defined: bool = False) -> str:
    if not isinstance(source, str) or len(source) > limits.max_input_bytes:
        raise ValueError("HTML input exceeds configured limit")
    if len(source.encode("utf-8")) > limits.max_input_bytes:
        raise ValueError("HTML input exceeds configured limit")
    parser = _Normalizer(limits, bool(server_defined))
    try:
        parser.feed(source)
        parser.close()
    except DocumentRenderError:
        raise
    except ValueError:
        raise
    except Exception:
        raise ValueError("HTML input is malformed") from None
    if parser.open_fields:
        raise ValueError("Malformed DocuSeal field markup")
    return "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>" + "".join(parser.output) + "</body></html>"


def _validate_public_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        parsed.port  # Force malformed-port validation while allowing explicit HTTPS ports.
        valid = (parsed.scheme == "https" and bool(parsed.hostname)
                 and parsed.username is None and parsed.password is None
                 and not parsed.path and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("ARTIFACT_PUBLIC_BASE_URL must be an HTTPS origin without path, query, or credentials")
    return value.rstrip("/")


async def _call_renderer(renderer: Callable[..., Any], normalized: str, size: str) -> bytes:
    result = renderer(normalized, document_size=size)
    if asyncio.iscoroutine(result):
        result = await result
    if not isinstance(result, bytes):
        raise DocumentRenderError("Document renderer returned invalid output")
    return result
async def _terminate_worker(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except AttributeError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    wait_task = asyncio.create_task(process.wait())
    cancellation_received = False
    while not wait_task.done():
        try:
            await asyncio.shield(wait_task)
        except asyncio.CancelledError:
            cancellation_received = True
    wait_task.result()
    if cancellation_received:
        raise asyncio.CancelledError



def _encode_worker_payload(html_content: str, document_size: str,
                           limits: RenderLimits, server_defined: bool) -> bytes:
    return json.dumps({
        "html": html_content,
        "size": document_size,
        "limits": vars(limits),
        "server_defined": bool(server_defined),
    }).encode()


def _decode_worker_result(stdout: bytes) -> tuple[bytes, bytes]:
    result = json.loads(stdout)
    if result.get("error") == "invalid_input":
        raise ValueError("HTML input is invalid or exceeds configured limits")
    if result.get("error") == "invalid_image":
        raise DocumentRenderError("Embedded image is invalid or exceeds configured limits")
    return (
        base64.b64decode(result["pdf"], validate=True),
        base64.b64decode(result["docx"], validate=True),
    )
async def _decode_worker_result_off_loop(stdout: bytes) -> tuple[bytes, bytes]:
    decode_task = asyncio.create_task(
        asyncio.to_thread(_decode_worker_result, stdout)
    )
    try:
        return await asyncio.shield(decode_task)
    except asyncio.CancelledError:
        while not decode_task.done():
            try:
                await asyncio.shield(decode_task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        raise


def _remove_worker_scratch_directory(scratch: Path) -> None:
    try:
        shutil.rmtree(scratch)
    except FileNotFoundError:
        return
    except OSError:
        logger.warning(
            "worker_scratch_cleanup_failed",
            extra={"operation": "cleanup", "error_class": "OSError"},
        )




async def _remove_worker_scratch(scratch: Path) -> None:
    cleanup_task = asyncio.create_task(
        asyncio.to_thread(_remove_worker_scratch_directory, scratch)
    )
    try:
        await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        raise


async def _subprocess_worker(html_content: str, document_size: str, timeout: float,
                             limits: RenderLimits,
                             server_defined: bool = False) -> tuple[bytes, bytes]:
    scratch = Path(tempfile.mkdtemp(prefix="docuseal-worker-"))
    process = None
    worker_identity = {}
    try:
        payload = await asyncio.to_thread(
            _encode_worker_payload, html_content, document_size, limits, server_defined
        )
        os.chmod(scratch, 0o700)
        if os.geteuid() == 0:
            import pwd
            account = pwd.getpwnam("nobody")
            os.chown(scratch, account.pw_uid, account.pw_gid)
            worker_identity = {
                "user": account.pw_uid,
                "group": account.pw_gid,
                "extra_groups": (),
            }
        spawn_task = asyncio.create_task(asyncio.create_subprocess_exec(
            os.sys.executable,
            "-m",
            "document_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONPATH": os.getcwd(),
                "PYTHONIOENCODING": "utf-8",
                "TMPDIR": str(scratch),
            },
            start_new_session=True,
            **worker_identity,
        ))
        try:
            process = await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            while not spawn_task.done():
                try:
                    await asyncio.shield(spawn_task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if spawn_task.done() and not spawn_task.cancelled():
                try:
                    process = spawn_task.result()
                except Exception:
                    pass
            raise
        stdout, _ = await asyncio.wait_for(process.communicate(payload), timeout)
        if process.returncode != 0:
            raise DocumentRenderError("Document rendering failed")
        return await _decode_worker_result_off_loop(stdout)
    except (TimeoutError, asyncio.CancelledError):
        raise
    except (DocumentRenderError, ValueError):
        raise
    except Exception:
        raise DocumentRenderError("Document rendering failed") from None
    finally:
        try:
            if process is not None:
                await _terminate_worker(process)
        finally:
            await _remove_worker_scratch(scratch)


def _worker_limits() -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (MAX_WORKER_CPU_SECONDS, MAX_WORKER_CPU_SECONDS))
        resource.setrlimit(resource.RLIMIT_AS, (MAX_WORKER_MEMORY_BYTES, MAX_WORKER_MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_WORKER_FILE_BYTES, MAX_WORKER_FILE_BYTES))
        if os.geteuid() == 0:
            import pwd
            account = pwd.getpwnam("nobody")
            os.setgroups([])
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
    except (ImportError, KeyError, OSError, ValueError):
        raise RuntimeError("Worker resource isolation unavailable") from None


_render_lock = asyncio.Semaphore(1)
_pending_publications: set[asyncio.Task[Any]] = set()


async def _discard_cancelled_publication(
    store: ArtifactStore, publish_task: asyncio.Task[dict[str, str]]
) -> None:
    try:
        links = await asyncio.shield(publish_task)
    except (asyncio.CancelledError, Exception):
        return
    try:
        await asyncio.to_thread(store.discard_pair, links)
    except Exception:
        logger.warning(
            "document_render_cleanup_failed",
            extra={"operation": "cleanup", "error_class": "ArtifactCleanupError"},
        )

def _forget_pending_publication(task: asyncio.Task[Any]) -> None:
    _pending_publications.discard(task)


async def _publish_pair_off_loop(
    store: ArtifactStore, template_name: str, pdf_data: bytes, docx_data: bytes
) -> dict[str, str]:
    publish_task = asyncio.create_task(
        asyncio.to_thread(store.publish_pair, template_name, pdf_data, docx_data)
    )
    try:
        return await asyncio.shield(publish_task)
    except asyncio.CancelledError:
        cleanup_task = asyncio.create_task(
            _discard_cancelled_publication(store, publish_task)
        )
        _pending_publications.add(cleanup_task)
        cleanup_task.add_done_callback(_forget_pending_publication)
        raise


def _log_render_rejection(error: Exception) -> None:
    logger.info(
        "document_render_rejected",
        extra={"operation": "render", "error_class": type(error).__name__},
    )


async def render_template_documents(
    template_name: str,
    html_content: str,
    document_size: str = "Letter",
    *,
    store: ArtifactStore,
    public_base_url: str,
    pdf_renderer: Callable[..., Any] | None = None,
    docx_renderer: Callable[..., Any] | None = None,
    worker_runner: Callable[[str, str], Any] | None = None,
    limits: RenderLimits = RenderLimits(),
    server_defined: bool = False,
) -> dict[str, str]:
    try:
        if _render_lock.locked() or _pending_publications:
            raise ArtifactCapacityError("A document render is already active")
        if not isinstance(html_content, str) or len(html_content) > limits.max_input_bytes:
            raise ValueError("HTML input exceeds configured limit")
        try:
            if len(html_content.encode("utf-8")) > limits.max_input_bytes:
                raise ValueError("HTML input exceeds configured limit")
        except UnicodeEncodeError:
            raise ValueError("HTML input is malformed") from None
        if not isinstance(document_size, str) or not document_size or len(document_size) > 32:
            raise ValueError("document_size is invalid")
        origin = _validate_public_origin(public_base_url)
    except (ArtifactCapacityError, ValueError) as error:
        _log_render_rejection(error)
        raise

    async with _render_lock:
        try:
            async with asyncio.timeout(limits.worker_timeout_seconds):
                if worker_runner is not None:
                    normalized = await asyncio.to_thread(
                        normalize_html, html_content, limits, server_defined
                    )
                    result = worker_runner(normalized, document_size)
                    if asyncio.iscoroutine(result):
                        pdf_data, docx_data = await result
                    else:
                        pdf_data, docx_data = result
                elif pdf_renderer is not None or docx_renderer is not None:
                    normalized = await asyncio.to_thread(
                        normalize_html, html_content, limits, server_defined
                    )
                    pdf_func = pdf_renderer or _render_pdf
                    docx_func = docx_renderer or _render_docx
                    async with asyncio.TaskGroup() as group:
                        pdf_task = group.create_task(
                            _call_renderer(pdf_func, normalized, document_size)
                        )
                        docx_task = group.create_task(
                            _call_renderer(docx_func, normalized, document_size)
                        )
                    pdf_data, docx_data = pdf_task.result(), docx_task.result()
                else:
                    pdf_data, docx_data = await _subprocess_worker(
                        html_content,
                        document_size,
                        limits.worker_timeout_seconds,
                        limits,
                        server_defined,
                    )
                if not isinstance(pdf_data, bytes) or not isinstance(docx_data, bytes):
                    raise DocumentRenderError("Document renderer returned invalid output")
                if len(pdf_data) > limits.max_output_bytes or len(docx_data) > limits.max_output_bytes:
                    raise DocumentRenderError("Rendered output exceeds configured limit")
                links = await _publish_pair_off_loop(
                    store, template_name, pdf_data, docx_data
                )
        except (ArtifactCapacityError, ValueError) as error:
            _log_render_rejection(error)
            raise
        except Exception:
            logger.warning(
                "document_render_failed",
                extra={"operation": "render", "error_class": "DocumentRenderError"},
            )
            raise DocumentRenderError("Document rendering failed") from None
    logger.info("document_render_succeeded", extra={"operation": "render", "status_code": 201})
    return {
        "pdf_url": origin + links["pdf_url"],
        "docx_url": origin + links["docx_url"],
        "expires_at": links["expires_at"],
    }


def _render_pdf(source: str, *, document_size: str = "Letter",
                max_pages: int = 100) -> bytes:
    if document_size not in {"Letter", "Legal", "A4", "A3", "A5"}:
        raise ValueError("document_size is invalid")
    from weasyprint import CSS, HTML, URLFetcher

    stylesheet = CSS(string=f"@page {{ size: {document_size}; }}")
    document = HTML(
        string=source,
        url_fetcher=URLFetcher(allowed_protocols={"data"}),
    ).render(stylesheets=[stylesheet])
    if len(document.pages) > max_pages:
        raise ValueError("PDF exceeds configured page limit")
    buffer = io.BytesIO()
    document.write_pdf(buffer)
    return buffer.getvalue()


def _count_docx_pages(docx_data: bytes) -> int:
    from pypdf import PdfReader

    with tempfile.TemporaryDirectory(prefix="docuseal-docx-page-check-") as root:
        directory = Path(root)
        docx_path = directory / "document.docx"
        pdf_path = directory / "document.pdf"
        docx_path.write_bytes(docx_data)
        profile = (directory / "profile").as_uri()
        try:
            result = subprocess.run(
                [
                    "libreoffice",
                    f"-env:UserInstallation={profile}",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(directory),
                    str(docx_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=MAX_DOCX_PAGE_CHECK_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise DocumentRenderError("DOCX page validation failed") from None
        if result.returncode != 0 or not pdf_path.is_file():
            raise DocumentRenderError("DOCX page validation failed")
        try:
            return len(PdfReader(str(pdf_path), strict=True).pages)
        except Exception:
            raise DocumentRenderError("DOCX page validation failed") from None


def _render_docx(
    source: str, *, document_size: str = "Letter", max_pages: int = 100
) -> bytes:
    if document_size not in {"Letter", "Legal", "A4", "A3", "A5"}:
        raise ValueError("document_size is invalid")
    from html4docx import HtmlToDocx
    parser = HtmlToDocx()
    parser.options["images"] = True
    parser.options["fix-html"] = False
    document = parser.parse_html_string(source)
    page_dimensions = {
        "Letter": (8.5, 11), "Legal": (8.5, 14),
        "A4": (8.27, 11.69), "A3": (11.69, 16.54), "A5": (5.83, 8.27),
    }
    from docx.shared import Inches
    for section in document.sections:
        section.page_width = Inches(page_dimensions[document_size][0])
        section.page_height = Inches(page_dimensions[document_size][1])
    buffer = io.BytesIO()
    document.save(buffer)
    docx_data = buffer.getvalue()
    if _count_docx_pages(docx_data) > max_pages:
        raise ValueError("DOCX exceeds configured page limit")
    return docx_data


