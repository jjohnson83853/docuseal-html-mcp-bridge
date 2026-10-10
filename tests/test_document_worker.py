import base64
import io
import json
import runpy
import sys
from types import SimpleNamespace

import pytest

import document_export
from document_export import DocumentRenderError, RenderLimits, _decode_worker_result


def run_worker(monkeypatch, request):
    input_stream = io.BytesIO(json.dumps(request).encode("utf-8"))
    output_stream = io.StringIO()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=input_stream))
    monkeypatch.setattr(sys, "stdout", output_stream)
    monkeypatch.setattr(document_export, "_worker_limits", lambda: None)
    runpy.run_module("document_worker", run_name="__main__")
    return output_stream.getvalue()


def request_for(html="<p>safe</p>", max_output_bytes=1000):
    limits = vars(RenderLimits(max_output_bytes=max_output_bytes))
    return {"html": html, "size": "A4", "limits": limits}


def test_worker_main_returns_encoded_dual_format_output(monkeypatch):
    monkeypatch.setattr(document_export, "normalize_html", lambda html, limits, server_defined=False: "<p>safe</p>")
    monkeypatch.setattr(document_export, "_render_pdf", lambda *args, **kwargs: b"pdf-bytes")
    monkeypatch.setattr(document_export, "_render_docx", lambda *args, **kwargs: b"docx-bytes")

    result = json.loads(run_worker(monkeypatch, request_for()))

    assert base64.b64decode(result["pdf"], validate=True) == b"pdf-bytes"
    assert base64.b64decode(result["docx"], validate=True) == b"docx-bytes"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ValueError("bad markup"), {"error": "invalid_input"}),
        (DocumentRenderError("bad image"), {"error": "invalid_image"}),
    ],
)
def test_worker_main_returns_safe_normalization_errors(monkeypatch, failure, expected):
    def fail_normalize(html, limits, server_defined=False):
        raise failure

    monkeypatch.setattr(document_export, "normalize_html", fail_normalize)

    assert json.loads(run_worker(monkeypatch, request_for())) == expected


def test_worker_main_rejects_output_over_configured_limit(monkeypatch):
    monkeypatch.setattr(document_export, "normalize_html", lambda html, limits, server_defined=False: "<p>safe</p>")
    monkeypatch.setattr(document_export, "_render_pdf", lambda *args, **kwargs: b"too large")
    monkeypatch.setattr(document_export, "_render_docx", lambda *args, **kwargs: b"docx")

    with pytest.raises(ValueError, match="Rendered output exceeds configured limit"):
        run_worker(monkeypatch, request_for(max_output_bytes=4))



def test_worker_result_decoder_returns_bytes_and_maps_safe_error_responses():
    result = _decode_worker_result(
        json.dumps({
            "pdf": base64.b64encode(b"pdf").decode("ascii"),
            "docx": base64.b64encode(b"docx").decode("ascii"),
        }).encode("utf-8")
    )
    assert result == (b"pdf", b"docx")

    with pytest.raises(ValueError, match="HTML input is invalid"):
        _decode_worker_result(b'{"error":"invalid_input"}')
    with pytest.raises(DocumentRenderError, match="Embedded image is invalid"):
        _decode_worker_result(b'{"error":"invalid_image"}')