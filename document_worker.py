"""One-shot rendering entrypoint; launched without the parent process environment."""

from __future__ import annotations

import base64
import json
import sys

from document_export import (
    DocumentRenderError,
    RenderLimits,
    _render_docx,
    _render_pdf,
    _worker_limits,
    normalize_html,
)


def main() -> None:
    _worker_limits()
    request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    limits = RenderLimits(**request["limits"])
    try:
        normalized = normalize_html(request["html"], limits)
    except ValueError:
        sys.stdout.write(json.dumps({"error": "invalid_input"}))
        return
    except DocumentRenderError:
        sys.stdout.write(json.dumps({"error": "invalid_image"}))
        return
    pdf = _render_pdf(normalized, document_size=request["size"], max_pages=limits.max_pages)
    docx = _render_docx(
        normalized,
        document_size=request["size"],
        max_pages=limits.max_pages,
    )
    if len(pdf) > limits.max_output_bytes or len(docx) > limits.max_output_bytes:
        raise ValueError("Rendered output exceeds configured limit")
    sys.stdout.write(
        json.dumps(
            {
                "pdf": base64.b64encode(pdf).decode("ascii"),
                "docx": base64.b64encode(docx).decode("ascii"),
            }
        )
    )


if __name__ == "__main__":
    main()
