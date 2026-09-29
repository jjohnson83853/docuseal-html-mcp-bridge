"""LibreChat-facing Streamable HTTP MCP server for DocuSeal REST."""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from docuseal_api import DocuSealClient

DOCUSEAL_URL = os.getenv("DOCUSEAL_URL", "https://signature.bullprog.com").rstrip("/")
DOCUSEAL_API_KEY = os.getenv("DOCUSEAL_API_KEY", "")
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")

SERVER_INSTRUCTIONS = """Use DocuSeal HTML field elements to create templates. Generate complete HTML from the user's specification; do not invent a separate template format. Supported elements include <text-field>, <date-field>, <signature-field>, <initials-field>, <checkbox-field>, <radio-field>, and <select-field>. Give every field a descriptive name and signer role (for example role=\"Signer\"). Use required where appropriate and CSS style dimensions to position and size fields. Preserve the supplied HTML. Sending a submission contacts the signer and must only be done when the user has explicitly requested sending it."""

mcp = FastMCP(
    "DocuSeal REST bridge",
    instructions=SERVER_INSTRUCTIONS,
    host=MCP_HOST,
    port=8000,
    streamable_http_path="/mcp",
)
_api_client = DocuSealClient(DOCUSEAL_URL, DOCUSEAL_API_KEY)


@mcp.tool()
async def create_template_from_html(
    template_name: str, html_content: str, document_size: str = "Letter"
) -> dict:
    """Create a DocuSeal PDF template from tagged HTML. Fields should use named DocuSeal elements, signer roles, and CSS sizing/positioning."""
    return await _api_client.create_template_from_html(
        template_name, html_content, document_size
    )


@mcp.tool()
async def search_templates(query: str = "", limit: int = 10) -> dict:
    """Search templates by name and return DocuSeal template records including fields."""
    return await _api_client.search_templates(query, limit)


@mcp.tool()
async def get_template(template_id: int) -> dict:
    """Retrieve one DocuSeal template by its numeric template ID, including its field schema."""
    return await _api_client.get_template(template_id)


@mcp.tool()
async def list_submissions(
    template_id: int | None = None,
    status: str | None = None,
    query: str | None = None,
    limit: int = 10,
) -> dict:
    """List submissions, optionally filtered by template ID, status, and search query."""
    return await _api_client.list_submissions(template_id, status, query, limit)


@mcp.tool()
async def send_submission(
    template_id: int,
    signer_email: str,
    signer_name: str,
    signer_role: str = "Signer",
    field_values: dict[str, Any] | None = None,
) -> dict:
    """Send a signing request for a template to one signer; this may send a real email."""
    return await _api_client.send_submission(
        template_id, signer_email, signer_name, signer_role, field_values
    )


@mcp.tool()
async def get_submission_status(submission_id: int) -> dict:
    """Retrieve a submission by its numeric ID and return DocuSeal's current status."""
    return await _api_client.get_submission_status(submission_id)


def main() -> None:
    if not DOCUSEAL_API_KEY:
        raise RuntimeError("DOCUSEAL_API_KEY must be configured before starting the server")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
