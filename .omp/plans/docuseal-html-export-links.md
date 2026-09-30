# SpecDD: Export DocuSeal HTML as PDF and DOCX

**Status:** Implementation tasks 2–5 and 7 complete; Task 6 logging/Caddy guidance implemented. Current suite: 117 passed, 2 LibreOffice-dependent tests skipped, 90.57% total source coverage. Final local MCP/artifact smoke rendered actual PDF/DOCX and downloaded both; DOCX page-count validation was stubbed because LibreOffice is unavailable on this workstation. Updated independent reviews are in progress. Docker build unavailable (daemon not running); visual layout inspection, Caddy syntax/sink probes, Portainer/Loki checks, and deployment remain operator tasks.
**Created:** 2026-09-30  
**Related plan:** `.omp/plans/docuseal-html-mcp-bridge-librechat.md`

## 1. Requirements

### Goal

Let a LibreChat user ask the bridge to render model-generated contract HTML into downloadable PDF and DOCX files, then manually upload either file to DocuSeal without requiring Pro-only template-creation APIs.

### User-provided context

- The user reports a 401 when searching templates and a 404 when attempting HTML template creation.
- User says the credential configured in the bridge is the same one used for direct DocuSeal native MCP access.
- The user's chosen workflow is: “Instead of turning on pro mode lets make this mcp container provide links to a rendered docx and pdfs which I can then manually upload.”
- User requires the artifact download listener port to be configurable.

### Discovered facts

- LibreChat reaches this service over MCP Streamable HTTP at `/mcp`; the Compose service shares a private Docker network with LibreChat and publishes no host port.
- The current bridge calls DocuSeal REST with `X-Auth-Token`; official DocuSeal MCP guidance uses a bearer MCP token, while REST uses an API key in `X-Auth-Token`.
- Current bridge tool `create_template_from_html` calls `POST /api/templates/html`. DocuSeal's current FAQ lists this endpoint as Pro-only for on-premises use (or available on the Free Cloud Developer Sandbox).
- `html-for-docx` 1.2.0 documents HTML-to-DOCX conversion but only a CSS subset. WeasyPrint 70.0 documents HTML/CSS-to-PDF conversion and warns that untrusted HTML/CSS can cause resource-exhaustion and local/network resource access; a restrictive URL fetcher and resource limits are needed.
- DocuSeal's manual UI accepts uploaded PDF and DOCX files and allows the user to add fields in its editor. Automatic DOCX-tag-to-template-field conversion is documented as Pro-only; this feature therefore exports visible drafts and requires manual field placement.
- MCP SDK supports custom Starlette HTTP routes alongside its Streamable HTTP route; this enables download URLs from the same container.
- Loki returned zero bridge log entries for 2026-09-30 05:45–06:20 UTC. Portainer's read-only Docker logs query for 04:45–06:20 UTC also returned empty stdout/stderr, so no request method/path/status or upstream detail was captured; the user's timezone is unknown, so 06:03 local cannot be correlated.
- Portainer confirmed the `DOCUSEAL_API_KEY` variable is present but redacted; value, credential type, and validity were not inspected. Active image digest is `sha256:85e8dbef2d177c9a0512918ea8ad477fde46521636f1139f8d0a9134f40c25db`, container started `2026-09-29T22:30:13Z`; the mutable `latest` reference does not identify a source commit.

### Scope

**In scope**
- Replace the Pro-dependent HTML template-create tool with an offline HTML-to-PDF-and-DOCX export tool.
- Return one browser-download link for each output, plus explicit expiry information.
- Add an artifact-only download listener on configurable `ARTIFACT_PORT` (default `8001`) and short-lived artifact cleanup.
- Make the REST API key optional for local rendering; retain other REST-backed tools, which continue to require valid REST credentials when called.
- Add minimal redacted request/error observability sufficient to distinguish missing configuration, DocuSeal 401/403/404, and local rendering failures without logging credentials, request bodies, HTML, or artifact tokens.
- Document the browser-reachable artifact URL and reverse-proxy/network requirement.

**Out of scope**
- Calling DocuSeal during PDF/DOCX generation or creating DocuSeal templates automatically.
- Uploading files to DocuSeal, changing DocuSeal licensing, or sending signing requests.
- Guaranteeing identical PDF and DOCX layout; DOCX conversion supports a smaller HTML/CSS subset.
- Publishing the MCP listener to the Internet or adding general MCP-client authentication. The user explicitly requires a separate Internet-reachable artifact listener and chose expiring bearer links without additional Caddy authentication.

### Invariants

- Export requests make no DocuSeal HTTP call and work without `DOCUSEAL_API_KEY`.
- REST credentials remain server-side and are never returned or logged.
- HTML and generated files never enter logs or committed files.
- The render worker's normalized input cannot read local files or initiate outbound requests through either renderer.
- Artifact download tokens are unguessable, format-scoped bearer capabilities, expire, and cannot select filesystem paths.
- A returned absolute link is generated only from an explicitly configured browser-reachable HTTPS base URL; no container DNS default is presented as browser-accessible.
- `ARTIFACT_PORT` defaults to `8001`, must be in `1024–65535`, and must not equal MCP port `8000`; Compose and Caddy upstream configuration must use the same value.
- No live DocuSeal request or signer email is part of verification.

## 2. Design

### Tool contract

- Replace `create_template_from_html(template_name, html_content, document_size)` with `render_template_documents(template_name, html_content, document_size="Letter")`.
- Render HTML locally to `application/pdf` and `application/vnd.openxmlformats-officedocument.wordprocessingml.document`.
- Return `{pdf_url, docx_url, expires_at}`. The tool description must say these are upload-ready drafts, not DocuSeal-created templates; users manually upload them and place/verify fields in DocuSeal.
- Normalize documented field elements (`text-field`, `date-field`, `signature-field`, `initials-field`, `checkbox-field`, `radio-field`, `select-field`) to visible placeholders labelled with field name/type. Preserve ordinary document text and supported styles; reject malformed recognized fields and report unsupported markup/assets rather than silently dropping them.

### Conversion and storage

- Both converters receive the same bounded normalized HTML subset. Strip scripts, external stylesheets, CSS imports/URL references, SVG, and network/local file resources; unsupported images/assets are rejected or omitted with a visible safe warning. Keep any permitted embedded raster data under strict encoded-byte and pixel limits.
- PDF: WeasyPrint uses a custom URL fetcher that denies `http`, `https`, `file`, and other external resource schemes.
- DOCX: `html-for-docx` uses its documented supported HTML/CSS subset; unsupported styles/layout may differ from PDF and must be documented. Headless LibreOffice Writer paginates each generated DOCX to PDF before publication so `MAX_PAGES` applies to both formats; its PDF conversion runs as a child of the bounded worker, in the same killable process group, and is required at runtime.
- Run conversion in a killable non-root worker with no API secrets in its environment, bounded wall-clock/CPU/memory/input/output/page limits, and a private scratch/output directory. Permit at most one active render worker; reject concurrent render requests immediately with a safe capacity error rather than queueing unbounded work.
- Publish both completed formats atomically only after both conversions succeed. Expiry is a maximum lifetime, not a guarantee across process/container restart; startup removes stale/incomplete artifacts. Cleanup must not remove an artifact while an active download has it open.
- Store outputs in an ephemeral private temporary directory under server-generated IDs. Give each file its own cryptographically random token bound to that format; never accept a user filesystem path.
- Default artifact retention to 24 hours and reject configuration above 24 hours. Enforce aggregate budgets (default 256 MiB and 100 files including staged outputs); clean expired files first, then reject a new render safely if count/storage budget remains exhausted. Do not evict unexpired artifacts.

### HTTP and access boundary

- Run two distinct ASGI listeners in the same container: MCP only on port `8000`, on the trusted internal LibreChat/Caddy Docker networks; artifact downloads only on configurable `ARTIFACT_PORT` (default `8001`, never `8000`). Neither listener exposes the other listener's routes.
- The Caddy Docker network is trusted internal infrastructure: peers attached to it may reach both container listeners. Internet clients can reach only the Caddy routes explicitly configured for `ARTIFACT_PORT`; no host port is published, and Caddy must never proxy `8000`.
- The artifact listener exposes only `GET /download?token={opaque_token}&format={pdf|docx}`. It serves fixed content types, `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`, and `Referrer-Policy: no-referrer`.
- `ARTIFACT_PUBLIC_BASE_URL` is required for export links and must be a browser-reachable HTTPS origin without credentials, path, query, or fragment. Link URLs use this origin plus `/download`; tokens are query parameters so Caddy can redact them.
- Compose attaches the bridge to LibreChat's existing network and Caddy's existing network, and exposes container ports `8000` and `${ARTIFACT_PORT:-8001}` only to those networks. Publish no host ports. Configure Caddy to reverse-proxy only to `docuseal-mcp-bridge:<the same ARTIFACT_PORT value>`; never proxy port `8000`.
- User-selected access policy: no additional Caddy authentication. Anyone who obtains a live token link can download that one format. Tokens have at least 256 bits of entropy; validated retention defaults to 24 hours and cannot exceed that maximum.
- Disable Uvicorn access logging on the artifact listener. Configure Caddy access logging to replace the `token` query parameter with a fixed redaction marker. Never log tokens, document HTML, Referer values, or generated bytes.
- Reject expired/unknown tokens, mismatched format/token pairs, and unsupported formats with 404. No directory listing, token enumeration endpoint, or user-controlled filesystem path.

### Observability


- Emit structured REST-operation logs with operation name, request/correlation ID, upstream hostname and path (not query/body), status code, duration, and safe error class.
- At startup, record only whether the REST API key is configured; never record its value or a hash.
- Ensure authentication errors distinguish REST 401/403 from other API failures without exposing upstream response bodies.
- Emit token-free render outcomes, cleanup-failure classes, and download status/duration/format; never emit raw URI, query, Referer, HTML, credentials, or artifact bytes.
- Configure Caddy logs to redact the artifact token in access entries and omit URI/query/Referer from proxy error/trace logs. Verify every enabled log sink with synthetic-token success, redirect, and failure probes.
- Configure bridge stdout/error logging to reach Portainer container logs and then Loki with stable `service_name=docuseal-mcp-bridge`. Verify a synthetic non-sensitive marker first in Portainer stdout/stderr and then in Loki. If absent from container logs, fix bridge logging; if present in Portainer but absent in Loki, add the narrow collector/label rule. Both current Loki and Portainer log queries returned empty.
- Verify the Portainer-deployed image digest and route configuration before claiming the live service is running the fix. The active digest and `DOCUSEAL_API_KEY` variable presence are known, but cannot be correlated to the reported request or a source commit. The reported request's timezone/event and the credential's value/type/validity remain unverified.

### Runtime/configuration


- `DOCUSEAL_API_KEY` becomes optional at startup; REST-backed tools return a clear safe configuration/auth error as applicable.
- Add `ARTIFACT_PUBLIC_BASE_URL`, `ARTIFACT_PORT` (default `8001`, range `1024–65535`, distinct from `8000`), Caddy network name, ephemeral artifact directory, maximum input/output/page limits, worker time/resource limits, `MAX_CONCURRENT_RENDERS` (default 1), aggregate artifact-byte/file budgets (defaults 256 MiB/100 files), and retention (default 24 hours, hard maximum 24 hours), all with safe validation/defaults.
- Update Docker OS packages for WeasyPrint's runtime dependencies and pin conversion dependencies.
- Validate and bound the suggested download filename before generating `Content-Disposition`; never interpolate raw `template_name` into an HTTP header.

## 3. Acceptance Criteria

- **AC-001 — Offline dual-format export:** Given valid tagged HTML and no REST API key, LibreChat calls the render tool and receives PDF and DOCX links; no DocuSeal request occurs. Verify with MCP in-process client and an assertion that a mock upstream transport receives zero calls.
- **AC-002 — Field visibility:** Given supported DocuSeal field tags, both generated outputs contain a visible field placeholder/label. Verify by extracting text from the PDF and DOCX in tests; manually inspect representative rendered pages before release.
- **AC-003 — Download behavior:** A valid unexpired PDF/DOCX URL downloads the corresponding bytes from the configured `ARTIFACT_PORT` with the correct MIME type and attachment filename. Unknown, expired, or unsupported-format tokens return 404; filesystem traversal strings cannot access files. The artifact listener does not expose `/mcp`; port `8000` does not expose downloads. Verify through isolated ASGI route tests at a non-default test port.
- **AC-004 — URL, worker, and capacity isolation:** Both conversion paths reject HTTP(S), `file://`, CSS imports/URLs, scripts, SVG, and unsupported resources without fetching them; permitted embedded raster data stays within byte/pixel limits. Oversized input/output, too many pages, CPU/memory/time limits, and worker failure produce no links or partial artifacts. A busy worker is rejected without unbounded queuing. Verify with local file/HTTP sentinels, bounded worker tests, and concurrent calls.
- **AC-005 — Atomic expiry/cleanup and storage budgets:** Links are returned only after both formats complete; failed conversion leaves no artifacts. Retention cannot exceed 24 hours. Expired artifacts are deleted without racing an active download, and restart may make links unavailable early. Reaching aggregate file/byte capacity produces a safe refusal without evicting unexpired files. Verify with a controlled clock, temp directory, concurrent-download cleanup, and storage-full tests.
- **AC-006 — Credential isolation and optionality:** Export startup and tool calls succeed with `DOCUSEAL_API_KEY` unset; REST calls do not. Opaque tokens appear only inside returned download URLs; no API key, document HTML, Referer, raw URI, or opaque token appears in bridge/Caddy logs or errors. Verify with synthetic credentials, returned-link assertions, and captured logs.
- **AC-007 — REST and artifact observability:** Mocked 401/403/404 responses produce distinct safe error classes/status context and correlation IDs without raw upstream body or credential leakage. Render/cleanup/download logs carry only allowlisted token-free fields; a synthetic non-sensitive marker is visible first in Portainer container stdout/stderr and then in Loki. Do not contact DocuSeal during implementation or acceptance; correlating an actual DocuSeal 401 is deferred until separately authorized and is not a release criterion for this feature.
- **AC-008 — Listener separation and browser link routing:** With `ARTIFACT_PUBLIC_BASE_URL` configured, returned URLs use that exact HTTPS origin plus `/download` and a format-scoped token. Compose publishes no host ports; app bind port, Compose service environment, Compose `expose`, and Caddy upstream all use the same configurable `ARTIFACT_PORT` value. Verify startup rejection before either listener starts for `8000`, `1023`, `65536`, and malformed/non-integer values, with no fallback to `8001`. Verify a non-default port end-to-end through Caddy using synthetic-token success/error downloads; inspect Caddy config and confirm an Internet browser cannot reach `/mcp`. Caddy redacts tokens from every enabled access/error/trace sink and never routes `8000`.
- **AC-009 — Regression:** Existing non-create REST tools and private Streamable HTTP initialization/discovery continue to work under mock-backed tests. Full suite coverage remains at least 90%.

## 4. Tasks

1. Only after the three user approvals, required critic PASS verdicts, and applicable SDD gate opening: write failing tests for every AC, including zero REST calls; invalid `ARTIFACT_PORT` values (`8000`, `1023`, `65536`, malformed/non-integer) rejected before either listener starts without fallback; extracted field labels in both formats; MIME type/attachment filename; unknown, expired, and mismatched-format tokens; traversal rejection; both-renderer HTTP/file sentinels; permitted image byte/pixel limits; input/output/page/worker bounds; concurrent-render refusal; atomic failure cleanup and concurrent download; 24-hour retention cap; aggregate file/byte capacity; missing-key REST errors; mocked 401/403/404 correlation IDs; and tokens only in returned URLs, never logs/errors.
2. Normalize the supported HTML/DocuSeal field subset; implement both converters with a killable, bounded non-root worker, no API secrets in its environment, and explicit resource blocking for both code paths.
3. Add ephemeral format-scoped artifact tokens, atomic publication, bounded cleanup, validated ≤24-hour retention, one-render-at-a-time refusal, aggregate 256-MiB/100-file budgets with safe full-capacity rejection, fixed safe filenames, and HTTPS-origin validation.
4. Add an artifact-only ASGI application on configurable `ARTIFACT_PORT` (default `8001`), coordinated with the existing MCP-only server on port `8000` in one container; verify independent routes, non-default port, and graceful startup/shutdown. Document that both internal networks are trusted, while only the configured artifact port is Internet-routed.
5. Remove the Pro-only `create_template_from_html` REST tool, register the offline export tool, and make REST credentials optional at startup while keeping remaining REST-backed tools safe.
6. Add redacted REST/render/cleanup/download observability; emit safe events to container stdout/stderr, verify Portainer visibility then Loki ingestion, fixing service logging before collector rules; disable artifact-listener access logs; prepare a concrete Caddy route and access/error-log redaction example. Include synthetic-token success/error checks for all enabled sinks.
7. Update dependencies, Dockerfile, Compose networks and configurable internal exposed port; install headless LibreOffice Writer and pin `pypdf` to measure DOCX page count and enforce the shared `MAX_PAGES` ceiling. Ensure worker cancellation kills the process group, including the pagination child. Set the Compose service's `ARTIFACT_PORT` environment from the same `${ARTIFACT_PORT:-8001}` value used by `expose`; document the Caddy upstream value. Update README (including Caddy config and manual field-placement limits), the original plan's supersession note, and this plan. Publish no host ports and add no durable document volume.
8. Run the full test suite and verify at least 90% combined coverage, local MCP initialization/discovery, actual PDF/DOCX render-and-download smoke, and final representative-layout inspection; complete security, general, and Python reviews. A local image build is permitted as verification after implementation approval when Docker is available; do not push images or deploy.
9. After the user/authorized operator applies the documented Caddy route/token-redaction config, builds and pushes the image through the user-controlled registry workflow, and redeploys the Portainer stack, hand off to `iac-devops` for read-only verification. Agents must never push to any registry. Verify deployed image digest, synthetic-token success/error requests through Caddy, no token in any enabled proxy log sink, synthetic marker in Portainer then Loki, and browser download while confirming `/mcp` is unreachable from the Internet. No live DocuSeal calls or signer emails.

## 5. Open Decisions / Deployment Prerequisites

- **Required before end-to-end link acceptance:** browser-reachable HTTPS Caddy origin routing only to configured `ARTIFACT_PORT` (default `8001`); port `8000` remains on trusted internal networks but is never Internet-routed. Peers on the Caddy Docker network can technically reach both ports, so that network is part of the trusted boundary.
- The user explicitly selected Internet-reachable expiring token links without additional Caddy authentication. Anyone obtaining a token URL can download its one file for up to 24 hours. Caddy must redact tokens from access/error/trace logs; Uvicorn must not log download requests. Process/container restart may invalidate links earlier.
- DocuSeal REST 401 cause remains inconclusive. Official docs indicate direct native MCP bearer tokens and REST API `X-Auth-Token` API keys are distinct credential mechanisms; using an MCP token as `DOCUSEAL_API_KEY` is the leading hypothesis, not a proven cause. 404 on `/api/templates/html` is consistent with the self-hosted Pro-only endpoint restriction but the live response cause/version remains unverified.

## 6. Sources

- DocuSeal native MCP authentication: https://www.docuseal.com/guides/integrate-with-ai-agents-using-mcp
- DocuSeal REST API: https://www.docuseal.com/docs/api
- DocuSeal Pro-only API endpoints: https://www.docuseal.com/faq/which-endpoints-are-available-only-with-the-pro-plan
- DocuSeal manual PDF/DOCX upload: https://www.docuseal.com/resources/quick-start
- DocuSeal tagged DOCX plan limitation: https://www.docuseal.com/resources/use-tagged-docx-with-fields
- WeasyPrint installation/security guidance: https://doc.courtbouillon.org/weasyprint/latest/first_steps.html
- `html-for-docx` package and supported HTML/CSS: https://pypi.org/project/html-for-docx/
- Caddy access-log filtering: https://caddyserver.com/docs/caddyfile/directives/log

## 7. Approval Ledger

### User checkpoints — this feature
- Requirements: APPROVED by user on 2026-09-30, including configurable `ARTIFACT_PORT`.
- Design: APPROVED by user on 2026-09-30.
- Tasks: APPROVED by user on 2026-09-30.

### Critic reviews
- SpecDD review: initial REVISE; amendments applied; final requirements/design review PASS.
- Saved task-list review: initial REVISE; amendments applied; final task review PASS.
- Final amendment review: PASS — includes mock-only 401/403/404 release verification and the user's Caddy/bearer-link decisions.
- Portainer/Loki evidence amendment review: PASS — observed state is distinguished from unverified request/image-source/credential details.
- Configurable-port amendment review: initial REVISE; amendments applied; final review PASS.

### Implementation gate
- Implementation gate: SATISFIED on 2026-09-30 — requirements, design, and tasks approved; required critic reviews PASS; `.superpowers/sdd/` workspace not present, so no `GATE_OPEN` marker applies.

### Implementation and verification outcome
- Publication writes, worker-payload encoding/result decoding, scratch cleanup, and download lease acquire/release run off the event loop. Cancelled publication cleanup removes an unpublished pair and blocks new renders until the background cleanup finishes.
- The Docker image runs the worker under UID 10001. A root-run parent launches the worker as `nobody` with no supplemental groups before interpreter startup; a non-root parent runs it under the bridge service UID. A compromised worker under the service UID can read bridge-owned unexpired artifacts; this accepted residual is documented in the README. The worker receives no REST secret environment variables.
- DOCX pagination is measured by headless LibreOffice Writer before publication; the page ceiling rejects either format when exceeded.
- Worker shutdown kills the process group even after its parent exits, then waits through repeated cancellation; subprocess creation is shielded so cancellation cannot orphan a just-spawned renderer. Scratch cleanup completes even if shutdown is cancelled. The root-only worker identity is applied at subprocess creation.
- Field placeholder styles that can hide or erase labels, including WebKit text fill/filter properties and unbounded spacing, are stripped while bounded layout styles remain; generated PDF/DOCX extraction verifies labels.
- Final local verification: `169 passed, 2 skipped`; total measured source coverage `90.61%`. The two skips require LibreOffice Writer; the only warning shared across tests is Pydantic's existing incomplete `lifespan` annotation warning (the real-PDF unit test also emits a HarfBuzz-Subset deprecation warning).
- Final scoped code, Python, and security reviews: no remaining actionable findings after the renderer-identity corrections. The root-path regression verifies subprocess identity kwargs and scratch ownership calls; an actual root-launched child was not smoke-tested because the local process is UID 1002 and Docker is unavailable.
- Local MCP client initialized protocol `2025-06-18`, discovered six tools, rendered actual PDF/DOCX with no REST key, and downloaded both through separate listeners; field-label visibility was confirmed in PDF and DOCX despite hiding styles. DOCX page-count validation was stubbed because LibreOffice Writer is unavailable locally. The page-count helper and overflow rejection are covered with mocked converter output.
- Docker daemon unavailable; no image build, actual LibreOffice pagination integration, deployed Caddy/Loki/Portainer probe, visual layout inspection, live DocuSeal request, signer email, registry push, or deployment was performed.
