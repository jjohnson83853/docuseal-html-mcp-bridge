import asyncio
import base64
import io
import re
import struct
import zlib
import zipfile
import shutil
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx
import pytest

import document_export
from artifact_server import create_artifact_app
from document_export import (
    ArtifactCapacityError,
    ArtifactStore,
    DocumentRenderError,
    RenderLimits,
    render_template_documents,
)


PUBLIC_BASE_URL = "https://downloads.example.test"
FIELD_HTML = """<html><body><h1>Agreement</h1>
<text-field name="Legal name" role="Signer"></text-field>
<date-field name="Effective date" role="Signer"></date-field>
<signature-field name="Signature" role="Signer"></signature-field>
<initials-field name="Initials" role="Signer"></initials-field>
<checkbox-field name="Agreed" role="Signer"></checkbox-field>
<radio-field name="Choice" role="Signer"></radio-field>
<select-field name="Jurisdiction" role="Signer"></select-field>
</body></html>"""
FIELD_LABELS = (
    "Legal name",
    "Effective date",
    "Signature",
    "Initials",
    "Agreed",
    "Choice",
    "Jurisdiction",
)

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def visible_text(html):
    parser = TextExtractor()
    parser.feed(html)
    return " ".join(parser.parts)




def run(coroutine):
    return asyncio.run(coroutine)


def get_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://artifact.test"
    )
def download_path(url):
    parsed = urlsplit(url)
    return f"{parsed.path}?{parsed.query}"
def test_download_token_is_unpredictable_and_never_logged(tmp_path, caplog):
    from urllib.parse import parse_qs

    caplog.set_level("INFO", logger="artifact_server")
    token = "synthetic-secret-token"
    store = ArtifactStore(tmp_path)
    links = store.publish_pair("Contract", b"private-pdf-bytes", b"private-docx-bytes")
    app = create_artifact_app(store)

    async def exercise():
        async with get_client(app) as client:
            path = download_path(links["pdf_url"])
            query = parse_qs(urlsplit(path).query)
            response = await client.get(path, headers={"Referer": token})
        assert response.status_code == 200
        assert len(query["token"][0]) >= 43

    run(exercise())
    assert token not in caplog.text
    assert "private-pdf-bytes" not in caplog.text
    assert links["pdf_url"].split("token=", 1)[1].split("&", 1)[0] not in caplog.text

    record = next(
        record for record in caplog.records
        if getattr(record, "operation", None) == "download"
    )
    assert record.status_code == 200
    assert record.duration_ms >= 0
    assert record.format == "pdf"




def test_render_returns_two_https_links_only_after_both_formats_finish(tmp_path):
    calls = []

    def pdf_renderer(html, **kwargs):
        calls.append(("pdf", html))
        return b"%PDF-1.7\nPDF fixture"

    def docx_renderer(html, **kwargs):
        calls.append(("docx", html))
        return b"PK\x03\x04DOCX fixture"

    store = ArtifactStore(tmp_path, clock=lambda: datetime(2026, 9, 30, tzinfo=timezone.utc))
    result = run(
        render_template_documents(
            "Mutual NDA",
            FIELD_HTML,
            store=store,
            public_base_url=PUBLIC_BASE_URL,
            pdf_renderer=pdf_renderer,
            docx_renderer=docx_renderer,
        )
    )

    assert set(result) == {"pdf_url", "docx_url", "expires_at"}
    assert result["pdf_url"].startswith(PUBLIC_BASE_URL + "/download?")
    assert result["docx_url"].startswith(PUBLIC_BASE_URL + "/download?")
    assert result["pdf_url"] != result["docx_url"]
    assert datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00")) == datetime(
        2026, 10, 1, tzinfo=timezone.utc
    )
    assert {kind for kind, _ in calls} == {"pdf", "docx"}
    normalized_text = [visible_text(html) for _, html in calls]
    assert all(all(label in text for label in FIELD_LABELS) for text in normalized_text)


def test_field_placeholders_preserve_safe_inline_styles_only(tmp_path):
    captured = []

    def renderer(normalized_html, **kwargs):
        captured.append(normalized_html)
        return b"rendered"

    html_content = (
        '<text-field name="Signer" '
        'style="display:block; margin-top:40px; margin-left:auto; '
        'font-weight:bold; font-style:italic; text-align:center"></text-field>'
        '<text-field name="Unsafe" '
        'style="background-image:url(https://example.invalid/x)"></text-field>'
    )

    run(
        render_template_documents(
            "Styled fields",
            html_content,
            store=ArtifactStore(tmp_path),
            public_base_url=PUBLIC_BASE_URL,
            pdf_renderer=renderer,
            docx_renderer=renderer,
        )
    )

    assert len(captured) == 2
    assert all(
        'style="display:block; margin-top:40px; margin-left:auto; '
        'font-weight:bold; font-style:italic; text-align:center"' in content
        for content in captured
    )
    assert all("background-image" not in content for content in captured)
    assert all("example.invalid" not in content for content in captured)


def test_hidden_field_styles_cannot_hide_required_placeholders(monkeypatch):
    from pypdf import PdfReader
    import document_export

    monkeypatch.setattr(document_export, "_count_docx_pages", lambda data: 1)
    normalized = document_export.normalize_html(
        '<text-field name="Visible signer" '
        'style="display:none; visibility:hidden; opacity:0; '
        'font-size:0; color:transparent; font-weight:9000; '
        '-webkit-filter:opacity(0); -webkit-text-fill-color:transparent; '
        '-webkit-mask-image:none; letter-spacing:-999em; '
        'margin-left:-9999px; margin-left:999999px; '
        'margin-top:5; margin-top:999999px; margin-top:40px"></text-field>',
        RenderLimits(),
        server_defined=True,
    )
    assert "display:none" not in normalized
    assert "visibility:hidden" not in normalized
    assert "opacity:0" not in normalized
    assert "font-size:0" not in normalized
    assert "; color:transparent;" not in normalized
    assert "-webkit-filter:" not in normalized
    assert "-webkit-text-fill-color:" not in normalized
    assert "-webkit-mask-image:" not in normalized
    assert "letter-spacing:" not in normalized
    assert "margin-left:" not in normalized
    assert "font-weight:9000" not in normalized
    assert "margin-top:5;" not in normalized
    assert "margin-left:999999px" not in normalized
    assert "margin-top:999999px" not in normalized
    assert "margin-top:40px" in normalized

    pdf = document_export._render_pdf(normalized)
    pdf_text = " ".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)
    docx = document_export._render_docx(normalized)
    with zipfile.ZipFile(io.BytesIO(docx)) as archive:
        xml = ElementTree.fromstring(archive.read("word/document.xml"))
    docx_text = " ".join(xml.itertext())
    assert "Visible signer" in pdf_text
    assert "Visible signer" in docx_text


def test_docx_page_limit_rejects_overflow_even_when_pdf_fits(monkeypatch):
    import document_export

    monkeypatch.setattr(
        document_export,
        "_count_docx_pages",
        lambda docx_data: 2,
        raising=False,
    )
    pdf = document_export._render_pdf("<p>Fits on one page</p>", max_pages=1)
    assert pdf.startswith(b"%PDF")

    with pytest.raises(ValueError, match="DOCX exceeds configured page limit"):
        document_export._render_docx(
            "<p>DOCX pagination exceeds the limit</p>",
            max_pages=1,
        )


def test_docx_page_counter_uses_isolated_headless_conversion(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from pypdf import PdfWriter
    import document_export

    def convert(command, **kwargs):
        assert command[0] == "libreoffice"
        assert command[1].startswith("-env:UserInstallation=file:")
        assert "--headless" in command
        assert kwargs["stdin"] == document_export.subprocess.DEVNULL
        assert kwargs["stdout"] == document_export.subprocess.DEVNULL
        output_dir = Path(command[command.index("--outdir") + 1])
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.add_blank_page(width=612, height=792)
        with (output_dir / "document.pdf").open("wb") as pdf:
            writer.write(pdf)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(document_export.subprocess, "run", convert)

    assert document_export._count_docx_pages(b"synthetic docx") == 2

def test_failed_second_conversion_publishes_no_downloadable_artifacts(tmp_path):
    store = ArtifactStore(tmp_path)

    def fail_docx(html, **kwargs):
        raise RuntimeError("renderer failed")

    with pytest.raises(DocumentRenderError):
        run(
            render_template_documents(
                "Mutual NDA",
                FIELD_HTML,
                store=store,
                public_base_url=PUBLIC_BASE_URL,
                pdf_renderer=lambda html, **kwargs: b"pdf bytes",
                docx_renderer=fail_docx,
            )
        )

    assert store.artifact_count == 0


def test_artifact_downloads_are_format_bound_and_secure(tmp_path):
    store = ArtifactStore(tmp_path)
    links = store.publish_pair("Mutual NDA", b"%PDF-fixture", b"docx-fixture")
    app = create_artifact_app(store)

    async def exercise():
        async with get_client(app) as client:
            pdf = await client.get(download_path(links["pdf_url"]))
            docx = await client.get(download_path(links["docx_url"]))
            mismatch_url = links["pdf_url"].replace("format=pdf", "format=docx")
            mismatch = await client.get(download_path(mismatch_url))
            unknown = await client.get("/download?token=unknown&format=pdf")
            unsupported = await client.get("/download?token=unknown&format=exe")
            traversal = await client.get(
                "/download?token=../../etc/passwd&format=pdf"
            )
            mcp_route = await client.get("/mcp")

        assert pdf.status_code == 200
        assert pdf.content == b"%PDF-fixture"
        assert pdf.headers["content-type"].startswith("application/pdf")
        assert "attachment" in pdf.headers["content-disposition"]
        assert "mutual-nda" in pdf.headers["content-disposition"].lower()
        assert pdf.headers["x-content-type-options"] == "nosniff"
        assert pdf.headers["cache-control"] == "no-store"
        assert pdf.headers["referrer-policy"] == "no-referrer"
        assert docx.status_code == 200
        assert docx.content == b"docx-fixture"
        assert "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in docx.headers[
            "content-type"
        ]
        assert all(response.status_code == 404 for response in (mismatch, unknown, unsupported, traversal))
        assert mcp_route.status_code == 404

    run(exercise())


def test_expired_token_is_unavailable_and_retention_cannot_exceed_one_day(tmp_path):
    now = [datetime(2026, 9, 30, tzinfo=timezone.utc)]
    store = ArtifactStore(
        tmp_path,
        clock=lambda: now[0],
        retention_seconds=24 * 60 * 60,
    )
    links = store.publish_pair("Contract", b"pdf", b"docx")
    now[0] += timedelta(days=1, seconds=1)

    async def exercise():
        async with get_client(create_artifact_app(store)) as client:
            response = await client.get(download_path(links["pdf_url"]))
        assert response.status_code == 404

    run(exercise())
    with pytest.raises(ValueError):
        ArtifactStore(tmp_path / "too-long", retention_seconds=24 * 60 * 60 + 1)


def test_public_origin_must_be_https_origin_without_path_or_credentials(tmp_path):
    renderer = lambda html, **kwargs: b"rendered"
    for invalid in (
        "http://downloads.example.test",
        "https://user:pass@downloads.example.test",
        "https://downloads.example.test/path",
        "https://downloads.example.test/?token=x",
    ):
        with pytest.raises(ValueError):
            run(
                render_template_documents(
                    "Contract",
                    "<p>Body</p>",
                    store=ArtifactStore(tmp_path / re.sub(r"\W", "_", invalid)),
                    public_base_url=invalid,
                    pdf_renderer=renderer,
                    docx_renderer=renderer,
                )
            )


def test_render_refuses_oversized_input_without_publishing(tmp_path):
    store = ArtifactStore(tmp_path)
    limits = RenderLimits(
        max_input_bytes=32,
        max_output_bytes=100_000,
        max_pages=10,
        max_image_bytes=100_000,
        max_image_pixels=100_000,
        worker_timeout_seconds=30,
    )
    renderer = lambda html, **kwargs: b"rendered"

    with pytest.raises(ValueError):
        run(
            render_template_documents(
                "Contract",
                "x" * 33,
                store=store,
                public_base_url=PUBLIC_BASE_URL,
                pdf_renderer=renderer,
                docx_renderer=renderer,
                limits=limits,
            )
        )

    assert store.artifact_count == 0


def test_artifact_budget_refuses_new_pair_without_evicting_live_files(tmp_path):
    store = ArtifactStore(tmp_path, max_files=2, max_bytes=100)
    original = store.publish_pair("First", b"pdf", b"docx")

    with pytest.raises(ArtifactCapacityError):
        store.publish_pair("Second", b"pdf", b"docx")

    async def exercise():
        async with get_client(create_artifact_app(store)) as client:
            pdf = await client.get(download_path(original["pdf_url"]))
            docx = await client.get(download_path(original["docx_url"]))
        assert (pdf.status_code, pdf.content) == (200, b"pdf")
        assert (docx.status_code, docx.content) == (200, b"docx")

    run(exercise())


def test_pdf_and_docx_renderers_cannot_reach_local_or_http_resources(tmp_path):
    sentinel = tmp_path / "secret.txt"
    sentinel.write_text("must-not-be-read", encoding="utf-8")
    attempted = []
    hostile_html = f"""<html><head>
      <script>window.secret = 'script-ran'</script>
      <link rel='stylesheet' href='http://127.0.0.1:9/style.css'>
      <style>@import url('http://127.0.0.1:9/import.css'); p {{ background:url('file://{sentinel}'); }}</style>
      </head><body><img src='file://{sentinel}'><svg><text>hidden</text></svg>
      <iframe src='http://127.0.0.1:9/secret'></iframe><p>Safe text</p></body></html>"""

    def renderer(html, **kwargs):
        attempted.append(html)
        return b"local rendered output"

    result = run(
        render_template_documents(
            "Contract",
            hostile_html,
            store=ArtifactStore(tmp_path / "artifacts"),
            public_base_url=PUBLIC_BASE_URL,
            pdf_renderer=renderer,
            docx_renderer=renderer,
        )
    )

    assert set(result) == {"pdf_url", "docx_url", "expires_at"}
    assert len(attempted) == 2
    assert all("must-not-be-read" not in html for html in attempted)
    assert all("script-ran" not in html for html in attempted)
    assert all("127.0.0.1:9" not in html for html in attempted)
    assert all("file://" not in html for html in attempted)
    assert all("@import" not in html.lower() for html in attempted)
    assert all("url(" not in html.lower() for html in attempted)
    assert all("<svg" not in html.lower() for html in attempted)
    assert all("<iframe" not in html.lower() for html in attempted)

def test_self_closing_unsupported_container_preserves_following_content(tmp_path):
    store = ArtifactStore(tmp_path)
    rendered = []

    def renderer(source, **kwargs):
        rendered.append(source)
        return b"rendered"

    run(
        render_template_documents(
            "Self closing",
            "<svg/><p>Agreement</p><text-field name='Signer' role='Signer'></text-field>",
            store=store,
            public_base_url=PUBLIC_BASE_URL,
            pdf_renderer=renderer,
            docx_renderer=renderer,
        )
    )

    assert "[Unsupported content omitted for safety]" not in rendered[0]
    assert "Agreement" in rendered[0]
    assert "Signer — Text field" in rendered[0]


def test_default_mode_preserves_model_css_and_omits_silently():
    normalized = document_export.normalize_html(
        "<style>p { font-family: Georgia; font-size: 14pt; }</style>"
        '<p style="font-family: Georgia; color: #333">Body</p>'
        "<script>evil()</script><svg>hidden</svg>",
        RenderLimits(),
    )
    assert "<style>p { font-family: Georgia; font-size: 14pt; }</style>" in normalized
    assert 'style="font-family: Georgia; color: #333"' in normalized
    assert "evil" not in normalized
    assert "<svg" not in normalized
    assert "hidden" not in normalized
    assert "Unsupported" not in normalized
    assert "omitted" not in normalized


def test_default_mode_drops_resource_loading_css():
    normalized = document_export.normalize_html(
        "<style>@import url(http://example.invalid/x.css); "
        "p { background: url(file:///etc/passwd); }</style><p>Body</p>",
        RenderLimits(),
    )
    assert "@import" not in normalized
    assert "url(" not in normalized
    assert "Body" in normalized


def test_server_defined_mode_strips_style_blocks_and_exotic_styles():
    normalized = document_export.normalize_html(
        '<style>p { color: red; }</style>'
        '<p style="color: blue; font-family: Arial">Body</p>',
        RenderLimits(),
        server_defined=True,
    )
    assert "<style" not in normalized
    assert "color: red" not in normalized
    assert "font-family" not in normalized
    assert "Body" in normalized


def test_render_server_defined_mode_strips_style(tmp_path):
    store = ArtifactStore(tmp_path)
    rendered = []

    def renderer(source, **kwargs):
        rendered.append(source)
        return b"rendered"

    run(
        render_template_documents(
            "Strict mode",
            "<style>p { font-family: Georgia; }</style><p>Agreement</p>",
            store=store,
            public_base_url=PUBLIC_BASE_URL,
            pdf_renderer=renderer,
            docx_renderer=renderer,
            server_defined=True,
        )
    )

    assert all("<style" not in source for source in rendered)
    assert all("font-family" not in source for source in rendered)


def test_artifact_store_publishes_pair_atomically_and_cleans_expired_files(tmp_path):
    now = [datetime(2026, 9, 30, tzinfo=timezone.utc)]
    store = ArtifactStore(tmp_path, clock=lambda: now[0], max_files=2)
    links = store.publish_pair("First", b"pdf", b"docx")
    now[0] += timedelta(days=1, seconds=1)
    assert store.cleanup_expired() == 2

    async def exercise():
        async with get_client(create_artifact_app(store)) as client:
            assert (await client.get(download_path(links["pdf_url"]))).status_code == 404
            assert (await client.get(download_path(links["docx_url"]))).status_code == 404

    run(exercise())
def test_malformed_recognized_field_tag_is_rejected_without_links(tmp_path):
    store = ArtifactStore(tmp_path)
    renderer = lambda html, **kwargs: b"rendered"

    with pytest.raises(ValueError):
        run(
            render_template_documents(
                "Contract",
                '<text-field role="Signer"></text-field>',
                store=store,
                public_base_url=PUBLIC_BASE_URL,
                pdf_renderer=renderer,
                docx_renderer=renderer,
            )
        )

    assert store.artifact_count == 0


def test_aggregate_artifact_budget_refusal_returns_no_links_or_partial_artifacts(
    tmp_path,
):
    store = ArtifactStore(tmp_path, max_bytes=8)
    renderer = lambda html, **kwargs: b"too-large-output"

    with pytest.raises(ArtifactCapacityError):
        run(
            render_template_documents(
                "Contract",
                "<p>Body</p>",
                store=store,
                public_base_url=PUBLIC_BASE_URL,
                pdf_renderer=renderer,
                docx_renderer=renderer,
            )
        )

    assert store.artifact_count == 0


def test_safe_error_does_not_include_html_or_renderer_exception_details(tmp_path, caplog):
    private_html = "<p>private-contract-content</p>"
    private_detail = "local-file=/var/secrets/key"

    def fail(html, **kwargs):
        raise RuntimeError(private_detail)

    with pytest.raises(DocumentRenderError) as error:
        run(
            render_template_documents(
                "Contract",
                private_html,
                store=ArtifactStore(tmp_path),
                public_base_url=PUBLIC_BASE_URL,
                pdf_renderer=fail,
                docx_renderer=lambda html, **kwargs: b"docx",
            )
        )

    captured = str(error.value) + caplog.text
    assert private_html not in captured
    assert private_detail not in captured
def test_only_one_render_worker_runs_at_a_time(tmp_path, monkeypatch, caplog):
    import document_export

    caplog.set_level("INFO", logger="document_export")
    store = ArtifactStore(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def worker(normalized_html, document_size):
        started.set()
        await release.wait()
        return b"pdf", b"docx"

    async def exercise():
        first = asyncio.create_task(
            render_template_documents(
                "First",
                FIELD_HTML,
                store=store,
                public_base_url=PUBLIC_BASE_URL,
                worker_runner=worker,
            )
        )
        await started.wait()
        monkeypatch.setattr(
            document_export,
            "normalize_html",
            lambda *args: pytest.fail("busy render performed HTML normalization"),
        )
        with pytest.raises(ArtifactCapacityError, match="already active"):
            await render_template_documents(
                "Second",
                FIELD_HTML,
                store=store,
                public_base_url=PUBLIC_BASE_URL,
                worker_runner=worker,
            )
        release.set()
        return await first

    result = run(exercise())
    assert set(result) == {"pdf_url", "docx_url", "expires_at"}
    assert store.artifact_count == 2
    rejection = next(
        record for record in caplog.records
        if record.getMessage() == "document_render_rejected"
    )
    assert rejection.error_class == "ArtifactCapacityError"
    assert FIELD_HTML not in caplog.text


def test_worker_deadline_cancels_runner_without_publishing_artifacts(tmp_path):
    store = ArtifactStore(tmp_path)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_worker(normalized_html, document_size):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def exercise():
        with pytest.raises(DocumentRenderError):
            await render_template_documents(
                "Contract",
                FIELD_HTML,
                store=store,
                public_base_url=PUBLIC_BASE_URL,
                worker_runner=blocked_worker,
                limits=RenderLimits(worker_timeout_seconds=0.01),
            )

    run(exercise())

    assert started.is_set()
    assert cancelled.is_set()
    assert store.artifact_count == 0


def test_worker_exit_also_kills_pagination_descendants():
    import os
    from pathlib import Path
    import signal
    import sys
    import document_export

    async def exercise():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            (
                "import subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(30)'],"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                "print(child.pid, flush=True)"
            ),
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        child_pid = int((await process.stdout.readline()).decode("ascii"))
        await process.wait()
        try:
            await document_export._terminate_worker(process)
            for _ in range(100):
                try:
                    state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
                except FileNotFoundError:
                    return True
                if state == "Z":
                    return True
                await asyncio.sleep(0.01)
            return False
        finally:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert run(exercise())


def test_subprocess_deadline_kills_and_reaps_worker_before_failure(
    tmp_path, monkeypatch
):
    import document_export

    class FakeProcess:
        returncode = None

        def __init__(self):
            self.started = asyncio.Event()
            self.killed = False
            self.waited = False

        async def communicate(self, payload):
            self.started.set()
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return self.returncode

    process = FakeProcess()

    async def create_process(*args, **kwargs):
        return process

    monkeypatch.setattr(document_export.asyncio, "create_subprocess_exec", create_process)
    store = ArtifactStore(tmp_path)

    async def exercise():
        with pytest.raises(DocumentRenderError):
            await render_template_documents(
                "Contract",
                FIELD_HTML,
                store=store,
                public_base_url=PUBLIC_BASE_URL,
                limits=RenderLimits(worker_timeout_seconds=0.01),
            )

    run(exercise())
    assert process.started.is_set()
    assert process.killed
    assert process.waited
    assert store.artifact_count == 0


def test_subprocess_decode_and_scratch_cleanup_do_not_block_event_loop(
    tmp_path, monkeypatch
):
    import threading
    import time
    import document_export

    class CompletedProcess:
        returncode = 0

        async def communicate(self, payload):
            return b"worker-response", None

        async def wait(self):
            return self.returncode

    decode_started = threading.Event()
    release_decode = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    original_rmtree = document_export.shutil.rmtree

    def decode(stdout):
        decode_started.set()
        assert release_decode.wait(2)
        return b"pdf", b"docx"

    def remove_scratch(path, ignore_errors=False):
        cleanup_started.set()
        assert release_cleanup.wait(2)
        original_rmtree(path, ignore_errors=ignore_errors)

    async def create_process(*args, **kwargs):
        return CompletedProcess()

    monkeypatch.setattr(document_export, "_decode_worker_result", decode)
    monkeypatch.setattr(document_export.shutil, "rmtree", remove_scratch)
    monkeypatch.setattr(document_export.asyncio, "create_subprocess_exec", create_process)

    async def assert_event_loop_responsive(ready, release):
        assert await asyncio.to_thread(ready.wait, 2)
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_later(0.02, heartbeat.set)
        release_timer = threading.Timer(0.5, release.set)
        release_timer.start()
        started = time.monotonic()
        try:
            await asyncio.wait_for(heartbeat.wait(), timeout=0.2)
            assert time.monotonic() - started < 0.2
        finally:
            release.set()
            release_timer.cancel()

    async def exercise():
        task = asyncio.create_task(
            document_export._subprocess_worker(
                "<p>safe</p>",
                "Letter",
                30,
                RenderLimits(max_output_bytes=1000, max_pages=1),
            )
        )
        await assert_event_loop_responsive(decode_started, release_decode)
        await assert_event_loop_responsive(cleanup_started, release_cleanup)
        return await task

    assert run(exercise()) == (b"pdf", b"docx")



def test_worker_scratch_cleanup_failure_is_logged_without_path_details(
    tmp_path, monkeypatch, caplog
):
    import document_export

    caplog.set_level("WARNING", logger="document_export")
    scratch = tmp_path / "worker"
    scratch.mkdir()
    sentinel = scratch / "private-output"
    sentinel.write_bytes(b"private")

    def fail_remove(path, ignore_errors=False):
        raise OSError("synthetic private path")

    monkeypatch.setattr(document_export.shutil, "rmtree", fail_remove)
    run(document_export._remove_worker_scratch(scratch))

    record = next(
        record for record in caplog.records
        if record.getMessage() == "worker_scratch_cleanup_failed"
    )
    assert record.error_class == "OSError"
    assert sentinel.exists()
    assert "synthetic private path" not in caplog.text


def test_cancelled_render_keeps_slot_until_worker_decode_finishes(
    tmp_path, monkeypatch
):
    import threading
    import document_export

    class CompletedProcess:
        returncode = 0

        async def communicate(self, payload):
            return b"worker-response", None

        async def wait(self):
            return self.returncode

    decode_started = threading.Event()
    release_decode = threading.Event()
    cleanup_started = threading.Event()

    def decode(stdout):
        decode_started.set()
        assert release_decode.wait(2)
        return b"pdf", b"docx"

    original_rmtree = document_export.shutil.rmtree

    def remove_scratch(path, ignore_errors=False):
        original_rmtree(path, ignore_errors=ignore_errors)
        cleanup_started.set()
    async def create_process(*args, **kwargs):
        return CompletedProcess()

    monkeypatch.setattr(document_export, "_decode_worker_result", decode)
    monkeypatch.setattr(document_export.shutil, "rmtree", remove_scratch)
    monkeypatch.setattr(document_export.asyncio, "create_subprocess_exec", create_process)
    store = ArtifactStore(tmp_path)

    async def exercise():
        render = asyncio.create_task(
            render_template_documents(
                "Contract",
                FIELD_HTML,
                store=store,
                public_base_url=PUBLIC_BASE_URL,
                limits=RenderLimits(worker_timeout_seconds=5),
            )
        )
        assert await asyncio.to_thread(decode_started.wait, 2)
        render.cancel()
        assert not await asyncio.to_thread(cleanup_started.wait, 0.1)
        assert not render.done()
        with pytest.raises(ArtifactCapacityError, match="already active"):
            await render_template_documents(
                "Second",
                FIELD_HTML,
                store=store,
                public_base_url=PUBLIC_BASE_URL,
            )
        release_decode.set()
        with pytest.raises(asyncio.CancelledError):
            await render

    try:
        run(exercise())
    finally:
        release_decode.set()
    assert store.artifact_count == 0

def test_repeated_cancellation_during_teardown_still_reaps_and_cleans(monkeypatch):
    import document_export

    class FakeProcess:
        returncode = 0

        def __init__(self):
            self.wait_started = asyncio.Event()
            self.release_wait = asyncio.Event()
            self.waited = False

        async def communicate(self, payload):
            return b"worker-response", None

        async def wait(self):
            self.wait_started.set()
            await self.release_wait.wait()
            self.waited = True
            return self.returncode

    process = FakeProcess()
    cleanup_finished = asyncio.Event()
    original_cleanup = document_export._remove_worker_scratch

    async def create_process(*args, **kwargs):
        return process

    async def decode(stdout):
        return b"pdf", b"docx"

    async def remove_scratch(path):
        await original_cleanup(path)
        cleanup_finished.set()

    monkeypatch.setattr(document_export.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(document_export, "_decode_worker_result_off_loop", decode)
    monkeypatch.setattr(document_export, "_remove_worker_scratch", remove_scratch)

    async def exercise():
        task = asyncio.create_task(
            document_export._subprocess_worker(
                "<p>safe</p>",
                "Letter",
                30,
                RenderLimits(max_output_bytes=1000, max_pages=1),
            )
        )
        await process.wait_started.wait()
        task.cancel()
        task.cancel()
        process.release_wait.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(exercise())
    assert process.waited
    assert cleanup_finished.is_set()


def test_cancelled_process_creation_finishes_then_kills_and_reaps_worker(monkeypatch):
    import document_export

    class FakeProcess:
        returncode = None
        killed = False
        waited = False

        async def communicate(self, payload):
            raise AssertionError("cancelled creation must not start communication")

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return self.returncode

    process = FakeProcess()
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()
    spawn_cancelled = asyncio.Event()

    async def create_process(*args, **kwargs):
        spawn_started.set()
        try:
            await release_spawn.wait()
        except asyncio.CancelledError:
            spawn_cancelled.set()
            raise
        return process

    monkeypatch.setattr(document_export.asyncio, "create_subprocess_exec", create_process)

    async def exercise():
        task = asyncio.create_task(
            document_export._subprocess_worker(
                "<p>safe</p>",
                "Letter",
                30,
                RenderLimits(max_output_bytes=1000, max_pages=1),
            )
        )
        await spawn_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        release_spawn.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(exercise())
    assert not spawn_cancelled.is_set()
    assert process.killed
    assert process.waited


@pytest.mark.parametrize(
    "parent_uid,expected_identity",
    (
        (0, {"user": 65534, "group": 65534, "extra_groups": ()}),
        (10001, {}),
    ),
)
def test_worker_spawn_uses_non_root_identity_only_for_root_parent(
    monkeypatch, parent_uid, expected_identity
):
    import document_export
    import pwd
    from types import SimpleNamespace

    spawned_options = {}
    chown_calls = []

    class FakeProcess:
        pid = 12345
        returncode = 0

        async def communicate(self, payload):
            return b"worker-response", None

        async def wait(self):
            return self.returncode

    process = FakeProcess()

    async def create_process(*args, **kwargs):
        spawned_options.update(kwargs)
        return process

    monkeypatch.setattr(document_export.os, "geteuid", lambda: parent_uid)
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_uid=65534, pw_gid=65534),
    )
    monkeypatch.setattr(
        document_export.os,
        "chown",
        lambda path, uid, gid: chown_calls.append((str(path), uid, gid)),
    )
    monkeypatch.setattr(document_export.os, "killpg", lambda *args: None)
    monkeypatch.setattr(document_export.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(
        document_export,
        "_decode_worker_result_off_loop",
        lambda stdout: asyncio.sleep(0, result=(b"pdf", b"docx")),
    )

    async def exercise():
        return await document_export._subprocess_worker(
            "<p>safe</p>",
            "Letter",
            30,
            RenderLimits(max_output_bytes=1000, max_pages=1),
        )

    assert run(exercise()) == (b"pdf", b"docx")
    identity_keys = ("user", "group", "extra_groups")
    actual_identity = {
        key: spawned_options[key] for key in identity_keys if key in spawned_options
    }
    assert actual_identity == expected_identity
    expected_chown = (
        [(spawned_options["env"]["TMPDIR"], 65534, 65534)]
        if expected_identity
        else []
    )
    assert chown_calls == expected_chown

def test_cancelled_subprocess_worker_is_killed_and_reaped(tmp_path, monkeypatch):
    import document_export

    class FakeProcess:
        returncode = None

        def __init__(self):
            self.wait_started = asyncio.Event()
            self.finish_wait = asyncio.Event()
            self.communicate_started = asyncio.Event()
            self.waited = False

        async def communicate(self, payload):
            self.communicate_started.set()
            await asyncio.Event().wait()

        def kill(self):
            self.returncode = -9

        async def wait(self):
            self.wait_started.set()
            await self.finish_wait.wait()
            self.waited = True
            return self.returncode

    process = FakeProcess()

    async def create_process(*args, **kwargs):
        return process

    monkeypatch.setattr(document_export.asyncio, "create_subprocess_exec", create_process)

    async def exercise():
        task = asyncio.create_task(
            document_export._subprocess_worker(
                "<p>safe</p>",
                "Letter",
                30,
                RenderLimits(max_output_bytes=1000, max_pages=1),
            )
        )
        await process.communicate_started.wait()
        task.cancel()
        await process.wait_started.wait()
        task.cancel()
        process.finish_wait.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(exercise())
    assert process.returncode == -9
    assert process.waited

def test_artifact_byte_budget_refuses_pair_independently_of_file_limit(tmp_path):
    store = ArtifactStore(tmp_path, max_files=10, max_bytes=5)

    with pytest.raises(ArtifactCapacityError):
        store.publish_pair("Too large", b"pdf", b"docx")

    assert store.artifact_count == 0
@pytest.mark.skipif(
    shutil.which("libreoffice") is None,
    reason="requires LibreOffice Writer for DOCX pagination",
)
def test_real_pdf_and_docx_downloads_contain_extracted_field_labels(tmp_path):
    from pypdf import PdfReader

    store = ArtifactStore(tmp_path)
    links = run(
        render_template_documents(
            "Field labels",
            FIELD_HTML,
            store=store,
            public_base_url=PUBLIC_BASE_URL,
        )
    )

    async def download():
        async with get_client(create_artifact_app(store)) as client:
            pdf_response = await client.get(download_path(links["pdf_url"]))
            docx_response = await client.get(download_path(links["docx_url"]))
        assert pdf_response.status_code == 200
        assert docx_response.status_code == 200
        return pdf_response.content, docx_response.content

    pdf_bytes, docx_bytes = run(download())
    pdf_text = " ".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf_bytes)).pages)
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as document:
        xml = ElementTree.fromstring(document.read("word/document.xml"))
    docx_text = " ".join(xml.itertext())

    for text in (pdf_text, docx_text):
        assert all(label in text for label in FIELD_LABELS)
@pytest.mark.skipif(
    shutil.which("libreoffice") is None,
    reason="requires LibreOffice Writer for DOCX pagination",
)
def test_real_renderers_do_not_fetch_loopback_or_file_resources(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    requests = []

    class SentinelHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"must-not-be-fetched")

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SentinelHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local_resource = tmp_path / "local-secret.txt"
    local_resource.write_text("must-not-be-read", encoding="utf-8")
    hostile_html = (
        f'<html><body><p>Safe text</p><img src="http://127.0.0.1:'
        f'{server.server_port}/resource"><img src="{local_resource.as_uri()}">'
        "</body></html>"
    )

    try:
        result = run(
            render_template_documents(
                "Resource isolation",
                hostile_html,
                store=ArtifactStore(tmp_path / "artifacts"),
                public_base_url=PUBLIC_BASE_URL,
            )
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert set(result) == {"pdf_url", "docx_url", "expires_at"}
    assert requests == []
def test_cleanup_preserves_expired_artifact_until_active_download_finishes(tmp_path):
    now = [datetime(2026, 9, 30, tzinfo=timezone.utc)]
    store = ArtifactStore(tmp_path, clock=lambda: now[0])
    links = store.publish_pair("Contract", b"pdf-download", b"docx")
    app = create_artifact_app(store)
    parsed = urlsplit(links["pdf_url"])
    body_started = asyncio.Event()
    allow_body = asyncio.Event()
    disconnected = asyncio.Event()
    sent_messages = []

    async def exercise():
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": parsed.path,
            "raw_path": parsed.path.encode(),
            "query_string": parsed.query.encode(),
            "headers": [(b"host", b"artifact.test")],
            "client": ("127.0.0.1", 12345),
            "server": ("artifact.test", 80),
            "root_path": "",
        }
        request_sent = False

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent_messages.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                body_started.set()
                await allow_body.wait()

        response_task = asyncio.create_task(app(scope, receive, send))
        await body_started.wait()
        now[0] += timedelta(days=1, seconds=1)
        assert store.cleanup_expired() == 0
        allow_body.set()
        disconnected.set()
        await response_task

    run(exercise())

    status = next(
        message["status"]
        for message in sent_messages
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in sent_messages
        if message["type"] == "http.response.body"
    )
    assert status == 200
    assert body == b"pdf-download"
    assert store.cleanup_expired() == 2
def png_data_uri(width, height):
    def chunk(kind, data):
        payload = kind + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))

    scanlines = b"".join(b"\0" + b"\0\0\0" * width for _ in range(height))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def test_embedded_image_over_byte_limit_is_rejected_without_publication(tmp_path):
    image = png_data_uri(1, 1)
    limits = RenderLimits(100_000, 100_000, 10, 1, 100, 1)
    store = ArtifactStore(tmp_path)

    with pytest.raises(DocumentRenderError):
        run(
            render_template_documents(
                "Contract", f'<img src="{image}">', store=store,
                public_base_url=PUBLIC_BASE_URL, limits=limits,
            )
        )

    assert store.artifact_count == 0


def test_embedded_image_over_pixel_limit_is_rejected_without_publication(tmp_path):
    image = png_data_uri(2, 2)
    limits = RenderLimits(100_000, 100_000, 10, 100_000, 3, 1)
    store = ArtifactStore(tmp_path)

    with pytest.raises(DocumentRenderError):
        run(
            render_template_documents(
                "Contract", f'<img src="{image}">', store=store,
                public_base_url=PUBLIC_BASE_URL, limits=limits,
            )
        )

    assert store.artifact_count == 0


def test_real_render_refuses_excess_pages_before_publication(tmp_path):
    html = "<p>First page</p><div style='page-break-before: always'></div><p>Second page</p>"
    limits = RenderLimits(100_000, 100_000, 1, 100_000, 100_000, 30)
    store = ArtifactStore(tmp_path)

    with pytest.raises(DocumentRenderError):
        run(
            render_template_documents(
                "Contract", html, store=store,
                public_base_url=PUBLIC_BASE_URL, limits=limits,
            )
        )

    assert store.artifact_count == 0


def test_per_output_limit_is_separate_from_store_capacity(tmp_path):
    store = ArtifactStore(tmp_path, max_bytes=1_000_000)
    limits = RenderLimits(100_000, 4, 10, 100_000, 100_000, 30)
    renderer = lambda html, **kwargs: b"five!"

    with pytest.raises(DocumentRenderError):
        run(
            render_template_documents(
                "Contract", "<p>Body</p>", store=store,
                public_base_url=PUBLIC_BASE_URL,
                pdf_renderer=renderer, docx_renderer=renderer, limits=limits,
            )
        )

    assert store.artifact_count == 0


@pytest.mark.parametrize(
    "field",
    (
        "max_input_bytes",
        "max_output_bytes",
        "max_pages",
        "max_image_bytes",
        "max_image_pixels",
        "worker_timeout_seconds",
    ),
)
@pytest.mark.parametrize("value", (0, -1, True, "1", float("nan"), float("inf")))
def test_render_limits_reject_invalid_values(field, value):
    values = dict(
        max_input_bytes=100_000,
        max_output_bytes=100_000,
        max_pages=10,
        max_image_bytes=100_000,
        max_image_pixels=100_000,
        worker_timeout_seconds=30,
    )
    values[field] = value

    with pytest.raises((TypeError, ValueError)):
        RenderLimits(**values)

def test_artifact_store_refuses_nonempty_unowned_root_without_mutating(tmp_path):
    tmp_path.chmod(0o755)
    sentinel = tmp_path / ("a" * 32)
    sentinel.write_text("unrelated", encoding="utf-8")

    with pytest.raises(ValueError, match="dedicated"):
        ArtifactStore(tmp_path)

    assert sentinel.read_text(encoding="utf-8") == "unrelated"
    assert tmp_path.stat().st_mode & 0o777 == 0o755
def test_failed_download_response_releases_expiry_lease(tmp_path):
    from urllib.parse import parse_qs, urlsplit
    from starlette.requests import Request
    from artifact_server import _download

    now = [datetime(2026, 9, 30, tzinfo=timezone.utc)]
    store = ArtifactStore(tmp_path, clock=lambda: now[0])
    links = store.publish_pair("Contract", b"pdf", b"docx")
    parsed = urlsplit(links["pdf_url"])
    token = parse_qs(parsed.query)["token"][0]
    app = create_artifact_app(store)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/download",
        "raw_path": b"/download",
        "query_string": parsed.query.encode(),
        "root_path": "",
        "headers": [],
        "server": ("test", 80),
        "client": ("test", 1234),
        "app": app,
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def fail_send(message):
        if message["type"] == "http.response.body":
            raise OSError("simulated client disconnect")

    async def exercise():
        response = await _download(Request(scope, receive))
        with pytest.raises(OSError, match="disconnect"):
            await response(scope, receive, fail_send)

    run(exercise())
    assert store.acquire(token, "pdf") is not None
    store.release(token)
    now[0] += timedelta(days=1, seconds=1)
    assert store.cleanup_expired() == 2


def test_image_decompression_warning_is_rejected(tmp_path, monkeypatch):
    import warnings
    from PIL import Image
    from document_export import _decode_image

    def warn_about_dimensions(data):
        warnings.warn("image dimensions exceed the decoder threshold", Image.DecompressionBombWarning)

    monkeypatch.setattr(Image, "open", warn_about_dimensions)
    with pytest.raises(DocumentRenderError):
        _decode_image(png_data_uri(1, 1), RenderLimits())


def test_expired_artifact_pair_is_retained_when_directory_cleanup_fails(
    tmp_path, monkeypatch
):
    import document_export

    now = [datetime(2026, 9, 30, tzinfo=timezone.utc)]
    store = ArtifactStore(tmp_path, clock=lambda: now[0])
    store.publish_pair("Contract", b"pdf", b"docx")
    now[0] += timedelta(days=1, seconds=1)

    def fail_cleanup(path):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(document_export.shutil, "rmtree", fail_cleanup)
    assert store.cleanup_expired() == 0
    assert store.artifact_count == 2


def test_partial_download_logs_actual_response_status(tmp_path, caplog):
    caplog.set_level("INFO", logger="artifact_server")
    store = ArtifactStore(tmp_path)
    links = store.publish_pair("Contract", b"0123456789", b"docx")
    path = download_path(links["pdf_url"])

    async def exercise():
        async with get_client(create_artifact_app(store)) as client:
            return await client.get(path, headers={"Range": "bytes=0-3"})

    response = run(exercise())
    record = next(
        record for record in caplog.records
        if getattr(record, "operation", None) == "download"
        and getattr(record, "status_code", None) == 206
    )
    assert response.status_code == 206
    assert response.content == b"0123"
    assert record.status_code == 206


def test_cancelled_render_keeps_capacity_reserved_until_publication_finishes(
    tmp_path, monkeypatch
):
    import threading
    import document_export

    store = ArtifactStore(tmp_path)
    publication_started = threading.Event()
    release_publication = threading.Event()

    def publish_pair(template_name, pdf_bytes, docx_bytes):
        publication_started.set()
        release_publication.wait()
        return {"pdf_url": "/pdf", "docx_url": "/docx", "expires_at": "later"}

    monkeypatch.setattr(store, "publish_pair", publish_pair)

    async def exercise():
        first = asyncio.create_task(
            render_template_documents(
                "First",
                "<p>first</p>",
                store=store,
                public_base_url=PUBLIC_BASE_URL,
                worker_runner=lambda normalized, size: (b"pdf", b"docx"),
            )
        )
        assert await asyncio.to_thread(publication_started.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(ArtifactCapacityError, match="already active"):
            await render_template_documents(
                "Second",
                "<p>second</p>",
                store=store,
                public_base_url=PUBLIC_BASE_URL,
                worker_runner=lambda normalized, size: (b"pdf", b"docx"),
            )
        release_publication.set()
        await asyncio.gather(*tuple(document_export._pending_publications))

    try:
        run(exercise())
    finally:
        release_publication.set()


def test_cancelled_real_publication_discards_completed_pair(tmp_path, monkeypatch):
    import threading
    import document_export

    store = ArtifactStore(tmp_path)
    write_started = threading.Event()
    release_write = threading.Event()
    original_write = document_export._write_private

    def blocking_write(path, data):
        write_started.set()
        assert release_write.wait(2)
        original_write(path, data)

    monkeypatch.setattr(document_export, "_write_private", blocking_write)

    async def exercise():
        render = asyncio.create_task(
            render_template_documents(
                "Contract",
                "<p>agreement</p>",
                store=store,
                public_base_url=PUBLIC_BASE_URL,
                worker_runner=lambda normalized, size: (b"pdf", b"docx"),
            )
        )
        assert await asyncio.to_thread(write_started.wait, 2)
        render.cancel()
        with pytest.raises(asyncio.CancelledError):
            await render
        with pytest.raises(ArtifactCapacityError, match="already active"):
            await render_template_documents(
                "Second",
                "<p>second</p>",
                store=store,
                public_base_url=PUBLIC_BASE_URL,
                worker_runner=lambda normalized, size: (b"pdf", b"docx"),
            )
        release_write.set()
        await asyncio.gather(*tuple(document_export._pending_publications))

    try:
        run(exercise())
    finally:
        release_write.set()
    assert store.artifact_count == 0
    assert [path for path in tmp_path.iterdir()
            if path.name != ".docuseal-artifact-store"] == []


def test_download_lease_lock_does_not_block_event_loop_during_publication(
    tmp_path, monkeypatch
):
    import threading
    import time
    import document_export

    store = ArtifactStore(tmp_path)
    existing = store.publish_pair("Existing", b"pdf", b"docx")
    write_started = threading.Event()
    release_write = threading.Event()
    original_write = document_export._write_private

    def blocking_write(path, data):
        write_started.set()
        assert release_write.wait(2)
        original_write(path, data)

    monkeypatch.setattr(document_export, "_write_private", blocking_write)
    app = create_artifact_app(store)

    async def exercise():
        render = asyncio.create_task(
            render_template_documents(
                "New",
                "<p>new</p>",
                store=store,
                public_base_url=PUBLIC_BASE_URL,
                worker_runner=lambda normalized, size: (b"new pdf", b"new docx"),
            )
        )
        assert await asyncio.to_thread(write_started.wait, 2)
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_later(0.02, heartbeat.set)
        release_timer = threading.Timer(0.5, release_write.set)
        release_timer.start()
        started = time.monotonic()
        try:
            async with get_client(app) as client:
                download = asyncio.create_task(
                    client.get(download_path(existing["pdf_url"]))
                )
                await asyncio.wait_for(heartbeat.wait(), timeout=0.2)
                assert time.monotonic() - started < 0.2
                response = await download
                assert response.status_code == 200
        finally:
            release_write.set()
            release_timer.cancel()
        await render

    run(exercise())
