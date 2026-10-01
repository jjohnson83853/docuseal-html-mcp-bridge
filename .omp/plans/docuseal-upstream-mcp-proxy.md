# SpecDD: Proxy DocuSeal Native MCP Through the Bridge

**Status:** Draft — requirements, design, and tasks require separate user approval; no implementation is authorized yet.
**Created:** 2026-09-30
**Refined:** 2026-09-30 (design/tasks grounded in repo facts; sequencing made explicit)
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
- The repository pins `mcp==1.16.0`. Its client rejected the native server's protocol `2025-11-25`; official Python SDK notes identify `mcp==1.23.1` as the protocol minimum and `mcp==1.24.0` as the minimum for the injectable `http_client` API. This plan selects `mcp==1.30.0` on the v1 line.
- LibreChat logs did not capture the 09:23 request target; the user's report that the template lookup succeeded is accepted as ground truth.
- The REST adapter's caller-facing result envelope is `{"data": [records], "pagination": {count, next}}`; limit is validated 1–100 at the boundary.
- Official DocuSeal self-hosted MCP documentation states `search_templates` requires `q`, accepts optional `limit` (default 10, maximum 100), and returns matching template IDs and names.

### Scope

**Draft in scope**
- Keep LibreChat connected only to `http://docuseal-mcp-bridge:8000/mcp`.
- Route the existing bridge `search_templates` tool through the upstream native MCP `search_templates` tool with Bearer authentication.
- Keep the bridge's existing tool name and caller-facing inputs; translate `query` to upstream `q` and pass the validated `limit` because the official native tool documentation advertises it.
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

### Upstream adapter — new file `docuseal_mcp.py`

- `DocuSealMcpClient.search_templates(query: str = "", limit: int = 10)` mirrors the REST adapter's signature so `server.py` routing stays a one-line swap for that tool only.
- Uses the official SDK v1 Streamable HTTP client (`streamablehttp_client` + `ClientSession`) with an injected `httpx.AsyncClient` carrying `Authorization: Bearer <DOCUSEAL_MCP_BEARER_TOKEN>` (injectable client API exists since mcp 1.24.0). Never uses `X-Auth-Token`.
- Session lifecycle: one open/close pair per `search_templates` call. No connection pool, no retry, no speculative keep-alive. A named constant bounds the total per-call timeout (default 20 s, matching the REST client's submission deadline style).
- Require a successful MCP `CallToolResult` (`isError` is false) with one text content block containing a JSON array of template records. Each record must contain an integer `id` and string `name`; an empty array is a valid no-match response. Return `{"data": [...]}` with only documented `id` and `name` fields; DocuSeal does not document pagination for this tool, so do not add a `pagination` object.
- Treat `isError`, missing/multiple/non-text content, invalid JSON, or invalid records as safe typed errors; do not include upstream response text, body, or token in error details.

### Routing — edit `server.py`

- After cutover, `search_templates` always calls the upstream MCP client; there is no configuration-dependent REST path or fallback. Missing token → bridge startup stays healthy; search raises a safe error naming the missing variable and never invokes REST.
- Validate `limit` 1–100 at the boundary exactly as `_validate_limit` does, and pass both `q=query` and `limit=limit` upstream; the official self-hosted MCP guide documents both parameters.

### Configuration

- `DOCUSEAL_MCP_URL` defaults to `https://signature.bullprog.com/mcp`; override for other native endpoints.
- `DOCUSEAL_MCP_BEARER_TOKEN` is read at bridge process startup and held only in server memory. It is never passed to child render workers, never logged, never returned in tool output, exceptions, or metrics.

### SDK pin

- `requirements.txt`: `mcp==1.16.0` → selected v1 pin `mcp==1.30.0` (protocol `2025-11-25` supported since 1.23.1; injectable authenticated `http_client` since 1.24.0). No migration to MCP v2.

### Verification design

- New `tests/test_docuseal_mcp.py` mocks Streamable HTTP/MCP session behavior (pattern: existing `httpx.MockTransport` and in-process MCP client tests): assert the bridge calls only the read-only upstream `search_templates` tool, maps `query` to `q`, forwards the validated `limit`, and returns only the normalized `id`/`name` records.
- Cover missing token, upstream `isError`, transport failure, malformed/missing/multiple content, invalid records, and cancellation; assert the token and raw upstream content never appear in errors, logs, or results.
- Extend `tests/test_server_runtime.py` and `tests/test_mcp_e2e.py` minimally to confirm the bridge always routes search through native MCP (including missing-token failure while a REST API key exists) and that the local render tool works with no DocuSeal credentials; existing REST-backed tool contract tests stay untouched.
- Smoke the bridge MCP endpoint with a mocked upstream; a live upstream smoke requires a fresh explicitly issued test credential and operator authorization. No Docker registry push or deployment is part of this work.

## 3. Acceptance Criteria

- **AC-001 — Bridge-only caller route:** LibreChat's configured target remains the bridge MCP endpoint; a bridge `search_templates` call then uses server-side Streamable HTTP to the configured native DocuSeal MCP endpoint. No direct LibreChat-to-DocuSeal request is required.
- **AC-002 — Bearer authentication:** The upstream call uses `Authorization: Bearer <DOCUSEAL_MCP_BEARER_TOKEN>` and never `X-Auth-Token`; tests inspect the header only in isolated mocks and assert it is absent from logs/errors/results.
- **AC-003 — Template lookup contract:** An empty query returns the upstream template list; a nonempty query maps to `q`; the bridge applies the approved envelope and limit rule without inventing totals or pagination.
- **AC-004 — Failure isolation:** Missing token, upstream auth rejection, unavailable upstream, malformed response, and cancellation produce bounded safe errors; rendering and bridge startup remain available.
- **AC-005 — Regression/security:** Existing REST-backed tool contracts and offline export behavior remain unchanged; no direct/upstream write tool is exposed; full suite coverage remains at least 90%.

## 4. Tasks

**Phase A — approvals (no code):**

1. **Requirements approval (user checkpoint 1).** Confirm scope is the existing `search_templates` tool only (recommended; all other bridge operations unchanged) and approve the result-envelope/limit contract above. *Acceptance:* approval recorded in the ledger.
2. **Design approval (user checkpoint 2).** Confirm the adapter design, SDK pin (`mcp==1.30.0`, stay on v1), configuration names, per-call session lifecycle, no-fallback rule, and unchanged downstream surface. *Acceptance:* approval recorded.
3. **Critic reviews then task-list approval (user checkpoint 3).** Run the SpecDD review and the saved task-list review; record verdicts; present the final task list for approval. *Acceptance:* both critic verdicts and user approval recorded; the authoritative DocuSeal schema and concrete result-decoding contract above are fixed before any implementation dispatch.

**Phase B — implementation (gated; only after Phase A):**

4. **SDK pin update.** Bump `requirements.txt` to `mcp==1.30.0`; confirm the v1 Streamable HTTP and injectable-client APIs import cleanly in the dev environment. *Acceptance:* pin updated, imports verified, no v2 packages introduced.
5. **Tests first (RED).** Write `tests/test_docuseal_mcp.py` covering: query→`q` and limit forwarding, Bearer header present in mock only, documented CallToolResult text-JSON decoding, valid empty results, malformed/error result rejection, missing token, upstream transport failure, cancellation, and token/redaction assertions. Add minimal routing checks in `test_server_runtime.py` and `test_mcp_e2e.py`. Run the new tests and confirm they fail for the right reason. *Acceptance:* RED confirmed.
6. **Implement (GREEN).** Add `docuseal_mcp.py`; edit `server.py` so `search_templates` always routes through the MCP client with startup-time config read and no REST fallback; keep `docuseal_api.py` and all other tools untouched. Re-run the exact new tests to green. *Acceptance:* RED tests GREEN; `render_template_documents` and REST tools unchanged.
7. **Docs and configuration.** Update `.env.example` with `DOCUSEAL_MCP_URL`/`DOCUSEAL_MCP_BEARER_TOKEN`; update README to distinguish the native MCP Bearer from REST `DOCUSEAL_API_KEY`, document query/limit behavior, and note the scope (search-only); keep secrets out of examples. *Acceptance:* docs consistent with shipped behavior.
8. **Verification and reviews.** Run focused tests, then the full suite with ≥90% coverage, and a mocked bridge MCP smoke. Complete code, Python, and security reviews (secret handling is a mandatory security check). A live upstream test is optional and requires a fresh operator-issued token; never reuse the earlier temporary token. Before any live upstream smoke or deployment, confirm that the earlier shared temporary token has been revoked/rotated; record status only, never the token. *Acceptance:* suite green at ≥90% coverage; reviews recorded; no live request without separate authorization.
9. **Commit and push** the verified implementation. No image build/push or deployment without separate authorization. *Acceptance:* committed per conventional-commit format; ledger updated.

## 5. Open Decisions for User Checkpoints

- **Scope (checkpoint 1):** route only `search_templates` through native MCP, or migrate every compatible existing DocuSeal operation? The native tool list observed was `search_templates`, `load_template`, `create_template`, `send_documents`, and `search_documents`; current bridge tools do not map one-to-one to all of them. **Recommended:** search-only now; preserve remaining bridge tools; expose no new write operations.
- **Default endpoint (checkpoint 2):** `DOCUSEAL_MCP_URL` defaults to the observed working `https://signature.bullprog.com/mcp` with an override variable. **Recommended:** confirm this default for deployment.

## 6. Approval Ledger

### User checkpoints
- Requirements: APPROVED by user on 2026-09-30 — route only `search_templates` through native MCP; keep other REST tools and offline rendering unchanged; expose no upstream write tools; forward query/limit; return only template `id`/`name` records in `data` without fabricated pagination.
- Design: PENDING
- Tasks: PENDING

### Critic reviews — complete
- SpecDD review: PASS — exact amendments applied; no remaining findings (2026-09-30).
- Saved task-list review: PASS — reviewed with SpecDD; no remaining findings (2026-09-30).

### Implementation gate
- CLOSED. No implementation dispatch is authorized until required critic verdicts and all three user approvals are recorded.

## 7. References

- Official DocuSeal API/MCP reference: https://www.docuseal.com/docs/api?lang=python
- Python MCP SDK v1.23.1 protocol support: https://github.com/modelcontextprotocol/python-sdk/releases/tag/v1.23.1
- Python MCP SDK v1.24.0 authenticated Streamable HTTP client API: https://github.com/modelcontextprotocol/python-sdk/releases/tag/v1.24.0
- Official DocuSeal self-hosted MCP guide and `search_templates` input/output contract: https://www.docuseal.com/guides/integrate-with-ai-agents-using-mcp
- Python MCP SDK v1.30.0 release notes: https://github.com/modelcontextprotocol/python-sdk/releases/tag/v1.30.0