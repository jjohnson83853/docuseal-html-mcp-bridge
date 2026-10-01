# SpecDD: Proxy DocuSeal Native MCP Through the Bridge

**Status:** Draft — requirements, design, and tasks require separate user approval; no implementation is authorized yet.
**Created:** 2026-10-01
**Related plan:** `.omp/plans/docuseal-html-export-links.md`

## 1. Requirements

### Goal

Make LibreChat use the private DocuSeal bridge as its only DocuSeal MCP endpoint, while the bridge calls the user's native DocuSeal MCP server server-to-server. Template search must work through the bridge using the native MCP Bearer credential; LibreChat must not connect directly to DocuSeal.

### User direction

- Direct LibreChat-to-DocuSeal MCP is out of bounds; requests must flow through this bridge.
- User supplied a temporary Bearer token for a one-time diagnostic. Never store it in the repository or reuse it after the diagnostic.
- The native DocuSeal MCP endpoint is `https://signature.bullprog.com/mcp`.

### Observed evidence

- Direct Streamable HTTP MCP to `/mcp` with `Authorization: Bearer` negotiated protocol `2025-11-25`; `search_templates` called with `q=""` returned six template IDs/names.
- The same temporary token sent as `X-Auth-Token` to `GET /api/templates` returned HTTP 401 `Not authenticated` through Caddy.
- This bridge currently exposes its own Streamable HTTP MCP endpoint at port 8000 `/mcp`; its `search_templates(query, limit)` currently calls the REST adapter, which sends `X-Auth-Token` to `/api/templates`.
- The repository pins `mcp==1.16.0`. Its client rejected the native server's protocol `2025-11-25`; official Python SDK notes identify `mcp==1.23.1` as the protocol minimum and `mcp==1.24.0` as the minimum for the injectable `http_client` API. `mcp==1.30.0` is the current v1 maintenance release.
- LibreChat logs did not capture the 09:23 request target; the user's report that the template lookup succeeded is accepted as ground truth.

### Scope

**Draft in scope**
- Keep LibreChat connected only to `http://docuseal-mcp-bridge:8000/mcp`.
- Route the existing bridge `search_templates` tool through the upstream native MCP `search_templates` tool with Bearer authentication.
- Keep the bridge's existing tool name and caller-facing inputs; translate `query` to upstream `q` and preserve limit behavior only to the extent the native tool schema supports it.
- Keep the offline `render_template_documents` tool local and independent of DocuSeal credentials.
- Keep other currently exposed bridge REST-backed operations unchanged unless separately approved; do not expose upstream `create_template` or other new write tools in this change.
- Add server-side `DOCUSEAL_MCP_URL` and `DOCUSEAL_MCP_BEARER_TOKEN` configuration; never log or return the token.
- Upgrade/pin the MCP SDK to a v1 release that supports protocol `2025-11-25` and authenticated Streamable HTTP clients.
- Mock all remote MCP interactions in tests; no live DocuSeal request or signer email in verification.

**Out of scope**
- Direct LibreChat-to-DocuSeal MCP access as the resolution.
- Changing the artifact renderer, artifact links, Caddy routes, or deployment.
- Proxying upstream template creation or document-sending tools.
- Replacing every REST-backed bridge operation unless approved as a separate requirement.

### Invariants

- All DocuSeal MCP calls made for this feature originate from the bridge; LibreChat targets only the bridge.
- The Bearer token is server-side only; it never reaches renderer workers, tool output, exceptions, or logs.
- Existing downstream `search_templates` tool name remains available; failures remain safe and do not leak upstream response bodies or credentials.
- A missing MCP token or unavailable upstream must not prevent local PDF/DOCX export or bridge startup; only the upstream-backed search fails safely.
- Existing REST-backed tools remain unchanged in this draft; a native MCP Bearer token is not treated as a REST `X-Auth-Token` API key.

## 2. Design

### Draft integration

- Add a small asynchronous upstream MCP adapter using the official Python SDK Streamable HTTP client and `Authorization: Bearer` header.
- Default `DOCUSEAL_MCP_URL` to `https://signature.bullprog.com/mcp`; allow configuration for the user's native MCP endpoint.
- Read `DOCUSEAL_MCP_BEARER_TOKEN` at bridge process startup and keep it only in server memory. Do not pass it to child render workers.
- For `search_templates(query, limit)`, call upstream `search_templates` with `q=query` and map the native MCP result into the existing bridge tool's documented result shape. Inspect/validate the upstream tool schema before deciding whether `limit` can be passed, applied locally, or needs a narrower caller contract; never silently fabricate pagination.
- Create/close the upstream MCP client session within the operation initially; no speculative connection pool or retries. Map transport/auth/tool failures to safe bridge errors and allowlist logs.
- Upgrade the pinned `mcp` package to a tested v1 version supporting `2025-11-25` and custom authenticated `httpx.AsyncClient` use; do not migrate to MCP v2 in this change.

### Verification design

- Deterministic tests mock Streamable HTTP/MCP session behavior; assert the bridge calls only the read-only upstream `search_templates` tool, maps `query` to `q`, preserves caller limits according to the confirmed upstream schema, and returns only the documented normalized result.
- Cover missing token, upstream 401, transport failure, malformed tool result, and cancellation without exposing the token or raw upstream content.
- Verify the local render tool still works with no API/MCP credentials and existing REST-backed tools retain their current contract.
- Smoke the bridge MCP endpoint with a mocked upstream; a live upstream smoke requires a fresh explicitly issued test credential and operator authorization. No Docker registry push or deployment is part of this work.

## 3. Acceptance Criteria

- **AC-001 — Bridge-only caller route:** LibreChat's configured target remains the bridge MCP endpoint; a bridge `search_templates` call then uses server-side Streamable HTTP to the configured native DocuSeal MCP endpoint. No direct LibreChat-to-DocuSeal request is required.
- **AC-002 — Bearer authentication:** The upstream call uses `Authorization: Bearer <DOCUSEAL_MCP_BEARER_TOKEN>` and never `X-Auth-Token`; tests inspect the header only in isolated mocks and assert it is absent from logs/errors/results.
- **AC-003 — Template lookup contract:** An empty query returns the upstream template list; a nonempty query maps to `q`; the bridge applies the final approved result envelope and limit semantics without inventing totals or pagination.
- **AC-004 — Failure isolation:** Missing token, upstream auth rejection, unavailable upstream, malformed response, and cancellation produce bounded safe errors; rendering and bridge startup remain available.
- **AC-005 — Regression/security:** Existing REST-backed tool contracts and offline export behavior remain unchanged; no direct/upstream write tool is exposed; full suite coverage remains at least 90%.

## 4. Tasks

1. Confirm requirements scope, including whether only `search_templates` is routed through native MCP or all currently exposed DocuSeal operations must be migrated, and approve the tool-result/limit contract.
2. Confirm design: authenticated server-to-server Streamable HTTP client, MCP SDK version, URL/token configuration, session lifecycle, errors/logging, and unchanged downstream surface.
3. Confirm the saved implementation task list before opening the implementation gate.
4. Write failing tests for bridge `search_templates` routing/mapping, bearer isolation, result normalization/limit boundaries, missing credentials, upstream 401/transport/malformed result, and cancellation. Re-run those exact tests for GREEN.
5. Implement the upstream MCP adapter, config wiring, SDK pin update, and search-tool routing. Preserve the local renderer and other REST tools unless approved scope says otherwise.
6. Update `.env.example`, README, and this plan to document native MCP URL/Bearer configuration and distinguish it from REST `DOCUSEAL_API_KEY`.
7. Run focused tests, full suite with at least 90% coverage, and mocked bridge MCP smoke. Complete code, Python, and security reviews. A live test is optional and requires a fresh test token; do not reuse the earlier temporary token.
8. Commit and push the verified implementation; no image build/push or deployment without separate authorization.

## 5. Open Decisions for User Checkpoints

- Is this change limited to the existing `search_templates` tool, or should every compatible existing DocuSeal tool be reimplemented through native MCP? The native tool list observed was `search_templates`, `load_template`, `create_template`, `send_documents`, and `search_documents`; current bridge tools do not map one-to-one to all of them. Recommended scope: route only `search_templates` now; preserve the remaining bridge tools and avoid exposing new write operations.
- Does the user-facing `limit` parameter need to remain meaningful when the native MCP tool does not advertise it? Upstream `search_templates` requires `q`; the full input schema was not retained during the credential test. Recommended: pass `limit` only if supported and otherwise make the reduced behavior explicit rather than fabricate pagination.
- Which native endpoint should be the default in deployment? Observed working endpoint: `https://signature.bullprog.com/mcp`; recommended default is that URL with an override variable.

## 6. Approval Ledger

### User checkpoints — pending
- Requirements: PENDING
- Design: PENDING
- Tasks: PENDING

### Critic reviews — pending
- SpecDD review: PENDING
- Saved task-list review: PENDING

### Implementation gate
- CLOSED. No implementation dispatch is authorized until required critic verdicts and all three user approvals are recorded.

## 7. References

- Official DocuSeal API/MCP reference: https://www.docuseal.com/docs/api?lang=python
- Python MCP SDK v1.23.1 protocol support: https://github.com/modelcontextprotocol/python-sdk/releases/tag/v1.23.1
- Python MCP SDK v1.24.0 authenticated Streamable HTTP client API: https://github.com/modelcontextprotocol/python-sdk/releases/tag/v1.24.0
- Python MCP SDK v1.30.0 release notes: https://github.com/modelcontextprotocol/python-sdk/releases/tag/v1.30.0
