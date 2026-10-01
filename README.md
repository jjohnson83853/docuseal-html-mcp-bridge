# DocuSeal HTML MCP bridge

This bridge provides LibreChat with private Streamable HTTP MCP access to DocuSeal template search, four REST operations, and an offline HTML-to-PDF/DOCX export tool. Export does not call DocuSeal and works without DocuSeal credentials. Exported documents are upload-ready drafts, not created DocuSeal templates; upload the PDF or DOCX manually and place/verify the fields in DocuSeal's editor. DOCX supports a smaller HTML/CSS subset than PDF, so layout may differ.

## Configuration and run

Python 3.11 or newer is required. Local renders require the `libreoffice` executable with Writer's PDF export filter for DOCX page-limit validation; the Docker image installs `libreoffice-writer-nogui`. DOCX exports fail closed when this validation is unavailable. Template search uses `DOCUSEAL_MCP_URL` (default `https://signature.bullprog.com/mcp`) and the server-only `DOCUSEAL_MCP_BEARER_TOKEN`; it calls native MCP with `Authorization: Bearer` and fails safely for search only if the token is missing or upstream is unavailable. It never falls back to REST. Other REST tools use `DOCUSEAL_URL` (default `https://signature.bullprog.com`) and optional `DOCUSEAL_API_KEY`, sent only in `X-Auth-Token`. These credentials are separate and are never interchangeable. `ARTIFACT_PUBLIC_BASE_URL` must be set to the exact browser-reachable HTTPS origin (no path, query, fragment, or credentials) before export can return links. `ARTIFACT_PORT` defaults to `8001`, must be between 1024 and 65535, and cannot be `8000`. `ARTIFACT_DIR` defaults…
Render/resource settings are configurable with `MAX_INPUT_BYTES`, `MAX_OUTPUT_BYTES`, `MAX_PAGES`, `MAX_IMAGE_BYTES`, `MAX_IMAGE_PIXELS`, and `WORKER_TIMEOUT_SECONDS`; aggregate storage uses `MAX_ARTIFACT_BYTES`, `MAX_ARTIFACT_FILES`, and `ARTIFACT_RETENTION_SECONDS` (maximum 86400). `MAX_CONCURRENT_RENDERS` must remain `1`; the worker has fixed 45-second CPU and 1-GiB address-space ceilings.

```sh
sudo apt-get install --no-install-recommends libreoffice-writer-nogui

python -m pip install -r requirements.txt
MCP_HOST=127.0.0.1 ARTIFACT_HOST=127.0.0.1 \
ARTIFACT_PUBLIC_BASE_URL=https://downloads.example.com python server.py
```

The MCP listener exposes only Streamable HTTP `/mcp` at port 8000. The artifact listener is a separate ASGI application on `ARTIFACT_PORT`, exposing only `GET /download`. Both bind addresses are configurable (`MCP_HOST`, `ARTIFACT_HOST`). The artifact port is configurable in Compose with the same value supplied to its environment and `expose` list.

Compose attaches the bridge only to the existing LibreChat and Caddy Docker networks and publishes no host port. These networks are trusted: a peer on the Caddy network can technically reach both listeners. Internet-facing Caddy must route only to the artifact listener; never proxy port 8000. Startup validates `ARTIFACT_PORT` before either listener starts. Both DocuSeal credentials may be omitted at startup; local rendering remains available, while only the feature using a missing credential fails.

```sh
cp .env.example .env
# Edit .env: set the real Docker networks and public HTTPS origin.
docker compose up --build -d
```

Compose reads `.env` automatically. `.env` is ignored by Git; keep credentials there and never commit it. Set `DOCUSEAL_MCP_BEARER_TOKEN` for template search and `DOCUSEAL_API_KEY` separately for REST-backed tools; neither is required for offline export.

LibreChat connects over its private network:

```yaml
mcpServers:
  docuseal:
    type: streamable-http
    url: http://docuseal-mcp-bridge:8000/mcp
    timeout: 60000
    serverInstructions: true
```

### Caddy routing and log redaction

Configure Caddy's default runtime logger to exclude `http.log.error` and `http.log.access`. Route `http.log.error` only through a named filtered runtime logger, and filter the site access logger separately. Both filters replace the `token` query value and delete `Referer`; the examples write to stdout. Apply equivalent filters to every other enabled sink. Caddy logging and filter docs: <https://caddyserver.com/docs/logging> and <https://caddyserver.com/docs/caddyfile/directives/log#filter-formats>.

```caddyfile
{
    log {
        output stdout
        exclude http.log.error http.log.access
    }
    log caddy_errors {
        include http.log.error
        output stdout
        format filter {
            wrap json
            fields {
                request>uri query {
                    replace token REDACTED
                }
                request>headers>Referer delete
            }
        }
    }
}

downloads.example.com {
    log {
        output stdout
        format filter {
            fields {
                request>uri query {
                    replace token REDACTED
                }
                request>headers>Referer delete
            }
        }
    }
    reverse_proxy docuseal-mcp-bridge:{$ARTIFACT_PORT}
}
```


The `ARTIFACT_PORT` value in the Caddy process must be the same value used by Compose. The global default logger excludes the raw HTTP error and access namespaces; the named error logger is the only configured runtime sink for `http.log.error`. Before deployment, probe synthetic-token successes, redirects, and upstream failures—including a synthetic `Referer` containing the same token-bearing URL—against every enabled Caddy log sink with debug/trace disabled; confirm no token appears. Bridge download access logging is disabled and bridge logs use allowlisted fields. Portainer-to-Loki ingestion and its stable `service_name=docuseal-mcp-bridge` label must be confirmed by the operator in the deployed environment; this repository change does not claim live Portainer/Loki verification.

## Available tools

- `render_template_documents(template_name, html_content, document_size="Letter")`: locally renders a PDF and DOCX and returns one format-bound, expiring download link per file plus `expires_at`. It never calls DocuSeal. Named `text-field`, `date-field`, `signature-field`, `initials-field`, `checkbox-field`, `radio-field`, and `select-field` elements become visible `[name — Type field]` labels. The seven field kinds require a non-empty `name`; styles that could hide or erase these labels are removed, while approved spacing, layout, and emphasis styles are preserved. Unsupported external resources are disabled/omitted. Users manually upload a returned file, then place and verify interactive fields in DocuSeal.
- `search_templates(query="", limit=10)`: searches DocuSeal's native read-only MCP `search_templates` tool through the bridge using one per-call connection, a 20-second deadline, and no retries. `query` maps to upstream `q`; `limit` must be 1–100. The result contains only `data` records with `id` and `name`; no pagination metadata is provided.
- `get_template`: retrieves `GET /api/templates/{id}`.
- `list_submissions`: filters `GET /api/submissions`.
- `send_submission`: posts to `POST /api/submissions`; this can email signers and is allowed only on explicit user request. A 20-second total request deadline applies; if the result is unknown, inspect DocuSeal before retrying.
- `get_submission_status`: retrieves `GET /api/submissions/{id}`.

Artifact links are bearer capabilities: anyone with a live link can fetch that one format until expiry. Tokens are random, format-scoped, and expire within 24 hours (default 24h). A process/container restart may invalidate them earlier. Downloads have fixed content type, attachment filename, `nosniff`, `no-store`, and `no-referrer` headers. Artifact storage is ephemeral, private, bounded to 256 MiB/100 files by default, and refuses new files rather than evicting unexpired artifacts. Only one render worker runs at a time; worker input/output, CPU, memory, and wall-clock time are bounded. The Docker image runs the worker under UID 10001. If the parent is launched as root, the child drops to the unprivileged `nobody` UID before interpreter startup, with no supplemental groups. A worker inheriting a non-root bridge service UID can read unexpired artifacts owned by that UID; this residual is accepted. The worker receives no REST secret environment variables. No host port is published.

Both renderers receive normalized HTML. Script, SVG, external stylesheets, CSS imports/resource URLs, external/local images and other unsupported active content are not loaded. Embedded raster images retained in the normalized subset are validated against byte and pixel ceilings before either converter sees them. WeasyPrint renders PDF; pinned `html-for-docx` produces DOCX. Unsupported layout/style fidelity is not guaranteed.

`DOCUSEAL_URL` must use HTTPS except for loopback development URLs. REST logs carry only operation, correlation ID, upstream hostname/path (without query/body), status, duration, and safe error class. API response bodies, credentials, HTML, tokens, raw download URIs, and Referer values are not returned in errors/logs.

## Tests

Install development dependencies and run `pytest`. Tests use mocked upstream transports and in-process ASGI clients; they must not contact DocuSeal or send signer emails. Verify local rendering/download behavior before deployment; Docker, Caddy log sinks, and deployed Portainer/Loki checks require an available runtime and operator authorization.
