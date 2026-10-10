# DocuSeal HTML MCP bridge

This bridge provides LibreChat with private Streamable HTTP MCP access to DocuSeal's native MCP tools and an offline HTML-to-PDF/DOCX export tool. Export does not call DocuSeal and works without DocuSeal credentials. Exported documents are upload-ready drafts, not created DocuSeal templates; upload the PDF or DOCX manually and place/verify the fields in DocuSeal's editor. DOCX supports a smaller HTML/CSS subset than PDF, so layout may differ.

## Configuration and run

Python 3.11 or newer is required. Local renders require the `libreoffice` executable with Writer's PDF export filter for DOCX page-limit validation; the Docker image installs `libreoffice-writer-nogui`. DOCX exports fail closed when this validation is unavailable. All retained DocuSeal operations use its native MCP server at the `/mcp` path derived from `DOCUSEAL_URL` (default origin `https://signature.bullprog.com`) and the server-only `DOCUSEAL_MCP_BEARER_TOKEN`, sent as `Authorization: Bearer`. `DOCUSEAL_URL` must be an HTTPS root origin except loopback development origins may use HTTP; only an optional trailing slash is accepted. The token is optional at startup so offline rendering remains available, but native MCP calls fail safely without it. The bridge never falls back to REST.
Render/resource settings are configurable with `MAX_INPUT_BYTES`, `MAX_OUTPUT_BYTES`, `MAX_PAGES`, `MAX_IMAGE_BYTES`, `MAX_IMAGE_PIXELS`, and `WORKER_TIMEOUT_SECONDS`; aggregate storage uses `MAX_ARTIFACT_BYTES`, `MAX_ARTIFACT_FILES`, and `ARTIFACT_RETENTION_SECONDS` (maximum 86400). `MAX_CONCURRENT_RENDERS` must remain `1`; the worker has fixed 45-second CPU and 1-GiB address-space ceilings.

```sh
sudo apt-get install --no-install-recommends libreoffice-writer-nogui

python -m pip install -r requirements.txt
MCP_HOST=127.0.0.1 ARTIFACT_HOST=127.0.0.1 \
ARTIFACT_PUBLIC_BASE_URL=https://downloads.example.com python server.py
```

The MCP listener exposes only Streamable HTTP `/mcp` at port 8000. The artifact listener is a separate ASGI application on `ARTIFACT_PORT`, exposing only `GET /download`. Both bind addresses are configurable (`MCP_HOST`, `ARTIFACT_HOST`). The artifact port is configurable in Compose with the same value supplied to its environment and `expose` list.

Compose attaches the bridge only to the existing LibreChat and Caddy Docker networks and publishes no host port. These networks are trusted: a peer on the Caddy network can technically reach both listeners. Internet-facing Caddy must route only to the artifact listener; never proxy port 8000. Startup validates `ARTIFACT_PORT` before either listener starts. Both DocuSeal values may be omitted at startup; local rendering remains available, while native MCP calls fail safely if the Bearer token is missing.

```sh
cp .env.example .env
# Edit .env: set the real Docker networks and public HTTPS origin.
docker compose up --build -d
```

Compose reads `.env` automatically. `.env` is ignored by Git; keep credentials there and never commit it. Set `DOCUSEAL_URL` to the DocuSeal root origin and `DOCUSEAL_MCP_BEARER_TOKEN` to its native MCP Bearer token; neither is required for offline export.

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

- `render_template_documents(template_name, html_content, document_size="Letter")`: locally renders a PDF and DOCX and returns one format-bound, expiring download link per file plus `expires_at`. It never calls DocuSeal. Named `text-field`, `date-field`, `signature-field`, `initials-field`, `checkbox-field`, `radio-field`, and `select-field` elements become visible `[name — Type field]` labels. The seven field kinds require a non-empty `name`; styles that could hide or erase these labels are removed. Unsupported elements and disabled resources are omitted silently (no placeholder text is emitted). Users manually upload a returned file, then place and verify interactive fields in DocuSeal.
- `search_templates(query="", limit=10)`: searches DocuSeal's native MCP `search_templates` tool through the bridge using one per-call connection, a 20-second deadline, and no retries. `query` maps to upstream `q`; `limit` must be 1–100. The result contains only `data` records with `id` and `name`; no pagination metadata is provided.
- `get_template(template_id)`: calls native MCP `load_template(template_id)` and returns the upstream native MCP success envelope (`content`, plus `structuredContent` when supplied), without reshaping its contents.
- `search_documents(q="", limit=10)`: calls native MCP `search_documents` with free-text `q` and `limit` (1–100). It returns the upstream native MCP success envelope. Template-ID and status filters are not supported.
- `send_submission(template_id, signer_email, signer_name, signer_role="Signer", field_values=None)`: calls native MCP `send_documents` and returns its native MCP success envelope. `field_values`, when supplied, are passed as named fields inside the one submitter. This can email signers and is allowed only on explicit user request. If response/transport is lost after dispatch, outcome is unknown; the bridge never retries and directs the caller to inspect DocuSeal before resending.

The former `list_submissions` filters and `get_submission_status` by-ID lookup are removed because no documented native MCP operations support them. The bridge does not expose `create_template`.

### Authoring prompts

Three MCP prompts help a calling model produce HTML that renders as expected (see `document_export.py` for the enforced rules):

- `document_html_guide`: full guide — allowed tags/attributes, the inline-styles rule (the DOCX converter ignores `<style>` blocks), the page-break idiom `<div style="page-break-after: always"></div>`, keeping blocks together (`break-inside: avoid` in PDF only; keep sections short for DOCX), and everything that is unavailable (JavaScript, SVG, iframes, external resources, web fonts, flexbox/grid in DOCX).
- `author_document_prompt(document_description)`: returns a ready-to-send authoring prompt embedding the hard requirements.
- `preflight_document_check(html_document)`: returns a checklist prompt that asks the model to fix violations (external URLs, hiding styles, missing field names, bad page breaks) before calling the render tool.


Artifact links are bearer capabilities: anyone with a live link can fetch that one format until expiry. Tokens are random, format-scoped, and expire within 24 hours (default 24h). A process/container restart may invalidate them earlier. Downloads have fixed content type, attachment filename, `nosniff`, `no-store`, and `no-referrer` headers. Artifact storage is ephemeral, private, bounded to 256 MiB/100 files by default, and refuses new files rather than evicting unexpired artifacts. Only one render worker runs at a time; worker input/output, CPU, memory, and wall-clock time are bounded. The Docker image runs the worker under UID 10001. If the parent is launched as root, the child drops to the unprivileged `nobody` UID before interpreter startup, with no supplemental groups. A worker inheriting a non-root bridge service UID can read unexpired artifacts owned by that UID; this residual is accepted. The worker receives no REST secret environment variables. No host port is published.

Both renderers receive normalized HTML. By default the calling model's CSS is rendered: `<style>` blocks and inline `style` attributes are passed through, with resource-loading CSS (`url()`, `@import`, `expression()`, `behavior:`, `-moz-binding`), backslashes, CSS comments, and `</style>` breakouts removed; `<style>` content is capped at 100 KB. Scripts, SVG, iframes, embedded objects, external stylesheets, external/local images, and other unsupported active content are never loaded and are omitted without any placeholder text; embedded raster images are validated against byte and pixel ceilings before either converter sees them. WeasyPrint renders PDF; pinned `html-for-docx` produces DOCX. Unsupported layout/style fidelity is not guaranteed.

Set `DOCUSEAL_SERVER_DEFINED_RENDERING=1` (also `true`/`yes`/`on`) to switch to the server-defined rendering profile: `<style>` blocks are stripped and inline styles fall back to the strict field-style whitelist (spacing, borders, display, font-weight/font-style, text-align); model-supplied `font-family`/`font-size` and other free-form CSS are then removed. The flag is read from the MCP server's environment at startup and cannot be changed per call.

`DOCUSEAL_URL` must be an HTTPS root origin except for loopback HTTP development URLs; the bridge derives the upstream `/mcp` endpoint. MCP errors are generic and redacted: credentials and raw upstream error content are not returned or logged. Artifact and rendering logs contain only allowlisted fields; HTML, tokens, raw download URIs, and Referer values are not returned in errors/logs.

## Tests

Install development dependencies and run `pytest`. Tests use mocked upstream transports and in-process ASGI clients; they must not contact DocuSeal or send signer emails. Verify local rendering/download behavior before deployment; Docker, Caddy log sinks, and deployed Portainer/Loki checks require an available runtime and operator authorization.
