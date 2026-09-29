# DocuSeal HTML MCP bridge

A small REST-backed MCP server for LibreChat. LibreChat is the only MCP client/front door; this bridge calls DocuSeal using its REST API and does not connect to DocuSeal's native MCP endpoint.

## Configuration and run

Set `DOCUSEAL_API_KEY` to a DocuSeal API key. `DOCUSEAL_URL` is optional and defaults to `https://signature.bullprog.com`. The bridge appends the required `/api/v1` path prefix and sends the key only to DocuSeal in the `X-Auth-Token` header.

Start locally with Python 3.11 or newer:

```sh
python -m pip install -r requirements.txt
DOCUSEAL_API_KEY=... python server.py
```

The MCP Streamable HTTP endpoint listens on port `8000` at `/mcp`. A direct Python run binds to `127.0.0.1` by default; Docker binds to `0.0.0.0` inside the shared container network without publishing a host port. Compose requires `DOCUSEAL_API_KEY` and the name of an existing Docker network shared with LibreChat:

```sh
LIBRECHAT_NETWORK=librechat_default DOCUSEAL_API_KEY=... docker compose up --build -d
```

LibreChat's YAML entry uses the bridge's service/container DNS name on that network:

```yaml
mcpServers:
  docuseal:
    type: streamable-http
    url: http://docuseal-mcp-bridge:8000/mcp
    timeout: 60000
    serverInstructions: true
```

Compose does not publish port `8000` on the host. The bridge endpoint has no client authentication; only attach trusted LibreChat services to the configured network. For local development, do not expose the listener to untrusted networks.

## Available tools

- `create_template_from_html`: sends `{name, html, size}` to `POST /api/v1/templates/html` and returns DocuSeal's template details. LibreChat should construct the HTML from the user's specification. Use named DocuSeal elements (such as `<text-field>`, `<date-field>`, and `<signature-field>`), set signer `role`, and use CSS styles to size and position fields. The bridge passes HTML through unchanged.
- `search_templates`: searches `GET /api/v1/templates`.
- `get_template`: retrieves `GET /api/v1/templates/{id}`.
- `list_submissions`: filters `GET /api/v1/submissions` by template, status, query, and limit.
- `send_submission`: posts to `POST /api/v1/submissions`. This may trigger signer notifications; call only on an explicit user request. The bridge enforces a 20-second total request deadline; if the outcome is unknown, inspect DocuSeal submissions before retrying.
- `get_submission_status`: retrieves `GET /api/v1/submissions/{id}`.

`DOCUSEAL_URL` must use HTTPS except for loopback development URLs. DocuSeal's published API documents routes without `/api/v1`; this bridge deliberately retains that required prefix. Compatibility with a particular on-prem DocuSeal target is not verified here. Do not use live submissions or assume target compatibility until validated against a safe sandbox and key.

## Tests

Install development dependencies and run `pytest`. The suite enforces at least 90% combined line coverage for the REST adapter and MCP server. Tests use `httpx.MockTransport` for DocuSeal calls and an in-process Streamable HTTP client; they do not contact DocuSeal or send signer emails.
