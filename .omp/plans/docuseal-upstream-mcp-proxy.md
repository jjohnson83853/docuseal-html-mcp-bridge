# SpecDD: Proxy DocuSeal Native MCP Through the Bridge

**Status:** DRAFT scope revision — Revision 2 (two-variable, native-MCP-only). Not approved; no code, no commit, no tests run, no live DocuSeal contact for this revision.
**Created:** 2026-09-30
**Revised:** 2026-10-01 (scope change: exactly two DocuSeal env values; all retained DocuSeal operations routed through native MCP)
**Related plan:** `.omp/plans/docuseal-html-export-links.md`

## 1. Scope-Change Summary

The user directed: *"Collapse these 4 values into 2."* Revision 1 (search-only) is superseded. The bridge must behave like LibreChat's native-DocuSeal integration: one origin plus one credential, with every retained DocuSeal operation served by the native MCP server at `<origin>/mcp`. The REST `X-Auth-Token` client (`docuseal_api.py`) and its configuration are removed, not re-pointed.

### Configuration contract — exactly two DocuSeal values

| Env value | Meaning | Becomes |
|---|---|---|
| `DOCUSEAL_URL` | Base DocuSeal origin (e.g. `https://signature.bullprog.com`) | **Kept.** Bridge derives the MCP endpoint as `{DOCUSEAL_URL}/mcp`. Same HTTPS/loopback validation rules as today; derivation rejects fragments/credentials/query. |
| `DOCUSEAL_API_KEY` | REST `X-Auth-Token` API key | **Removed** — all call sites, `.env.example`, `docker-compose.yml`, README, and startup logging. |
| `DOCUSEAL_MCP_URL` | Explicit `/mcp` endpoint | **Removed** — redundant because REST and MCP share the same origin in this deployment. |
| `DOCUSEAL_MCP_BEARER_TOKEN` | Native MCP `Authorization: Bearer` token | **Kept.** Server-side only; never logged, returned, or handed to render workers. Not reused as a REST key. |

One Bearer token in one auth scheme. The MCP Bearer is never treated as `X-Auth-Token`; no fallback between schemes.

## 2. Requirements

### User direction (Revision 2)

- Exactly two DocuSeal env values remain: `DOCUSEAL_URL` and `DOCUSEAL_MCP_BEARER_TOKEN`.
- All retained DocuSeal operations must use the native MCP tools; no REST adapter path survives the cutover.
- No new upstream write tools beyond what is needed to preserve existing behavior: `create_template` is **not** exposed.

### Hard rules

- Do not silently preserve unsupported contracts. Where the native MCP tool set does not document a parameter or capability the bridge tool advertised, the bridge tool is reshaped to the documented contract or removed — never approximated with fabricated behavior.
- Do not contact DocuSeal or run live requests in this revision. Mock everything in tests.
- Clean cutover: `docuseal_api.py`, its tests, and every `DOCUSEAL_API_KEY`/`DOCUSEAL_MCP_URL` reference are deleted when the last consumer is migrated; no shims, no dual-path config.

## 3. Tool-by-Tool Mapping

Current bridge tools and their Revision-2 disposition (current routing: `server.py:155-186`; REST client: `docuseal_api.py:67-131`):

| Bridge tool today | Current backend | Native MCP tool (documented inputs) | Disposition |
|---|---|---|---|
| `search_templates(query, limit)` | native MCP `search_templates` (already) | `search_templates(q, limit)` | **Unchanged.** Already correct; only the URL derivation changes. |
| `get_template(template_id)` | REST `_api_client.get_template` | `load_template(template_id)` | **Re-route to native MCP.** Same caller-facing input (`template_id: int`); returns template with field schema. Response-shape translation (REST record → MCP result content) to be grounded in the official guide's documented output before implementation; no invented fields. |
| `list_submissions(template_id?, status?, query?, limit)` | REST `_api_client.list_submissions` | `search_documents(q, limit)` | **Reshaped.** Native tool documents only `q` and `limit`. Template-ID, status, and free-text query filters have no documented native equivalent. See §4. |
| `send_submission(template_id, signer_email, signer_name, signer_role, field_values?)` | REST `_api_client.send_submission` | `send_documents(template_id, submitters)` | **Re-route to native MCP.** Caller-facing intent preserved (one signing request for one signer); map `field_values` to the documented `fields: [{name, value}]` array. The operation may send a real email. If timeout/transport loss occurs after the tool call was dispatched, return a safe outcome-unknown error, never retry automatically, and tell the caller to inspect DocuSeal before resending. |
| `get_submission_status(submission_id)` | REST `_api_client.get_submission_status` | none documented | **Removed.** No native tool or documented parameter fetches a submission by ID. See §4. |
| `render_template_documents` | local/offline | n/a | **Unchanged.** No DocuSeal credential, works with both new values unset. |

Not exposed: `create_template` (upstream write tool, no user approval).

## 4. Unsupported Semantics — Analysis and Recommendation

Two REST-backed capabilities lack a documented native MCP equivalent:

1. **Filtered submission listing.** `list_submissions` accepts `template_id`, `status`, and `query` filters. Native `search_documents` documents only `q` (free text) and `limit`. There is no documented way to filter by template ID or status upstream.
2. **Submission status by ID.** `get_submission_status(submission_id)` fetches a specific submission. No documented native tool or parameter performs a by-ID submission lookup.

### Options

- **Option A — Remove unsupported tools; keep documented-only shapes (RECOMMENDED).** Delete `get_submission_status` outright. Replace `list_submissions` with a new `search_submissions(q, limit)` tool backed by native `search_documents`, with the same `{"data": [...]}` envelope and 1–100 limit rule as `search_templates`. LibreChat callers using removed tools get a clear "tool not available" instead of degraded silent behavior.
- **Option B — Keep both REST tools and keep `DOCUSEAL_API_KEY`.** Satisfies none of the user's directive; rejected.
- **Option C — Keep tool names and silently map `template_id`/`status` filters into `q` text.** Fabricates unsupported upstream semantics; produces results that look like filtered output but are not. Rejected as dishonest interface behavior.
- **Option D — Keep `list_submissions`/`get_submission_status` as no-ops or stubs.** Violates no-stub delivery rules. Rejected.

### Why Option A is conservative

It preserves every capability the native server actually documents, removes nothing that has a native equivalent, and makes the two losses explicit and user-approved rather than hidden. If the user later needs by-ID status or filtered listing, that is a separate decision (e.g., confirm upstream additions), not part of this scope.

### Caller-visible behavior changes (Option A)

- `get_submission_status` disappears from the bridge tool list; attempts return a safe not-available error.
- `list_submissions` disappears; its replacement `search_submissions(q, limit)` returns free-text matches only. No `template_id`/`status` parameters exist on the new tool.
- `send_submission` keeps its name and inputs but runs upstream MCP; a malformed signer payload fails safely before any email effect, to the extent the documented upstream contract allows validation.
- `get_template` keeps its name and input; response fields are only those the documented MCP output provides.

## 5. Design (delta from Revision 1)

- `docuseal_mcp.py` keeps the per-call Streamable HTTP session lifecycle, Bearer-only auth, safe `CallToolResult` decoding, URL validation, and redaction guarantees; it gains `load_template`, `send_documents`, and `search_documents` methods mirroring documented native inputs, plus `/mcp` derivation from the base origin.
- `server.py` routes all four retained DocuSeal tools through `_mcp_client`; `_api_client` and the `DOCUSEAL_API_KEY`/`DOCUSEAL_MCP_URL` globals are deleted; `SERVER_INSTRUCTIONS` and startup logging drop REST-key references.
- Configuration: `DOCUSEAL_URL` must be a base origin with no path except `/`; derive `{origin}/mcp`; keep `DOCUSEAL_MCP_BEARER_TOKEN`. Validate HTTPS except loopback HTTP, no credentials/query/fragment, at startup.
- Envelope/limit rules stay as approved in Revision 1: validate 1–100 limits at the boundary, never fabricate pagination or totals, and return only fields documented for each native tool.
- The `send_submission` adapter must distinguish an explicit upstream tool error from an unknown outcome after a dispatched call loses its response. It never retries; the user-facing error directs operators to check DocuSeal before resending.
- SDK pin remains `mcp==1.30.0` (v1 line, protocol `2025-11-25`).
- Docs: `.env.example`, `docker-compose.yml`, and README rewritten for the two-variable contract; README explains LibreChat-parity behavior and which tools were removed and why.

## 6. Implementation Task List (gated — not started)

1. **Design approval (user checkpoint 1).** Confirm exactly-two-variable contract, Option A removals, `create_template` non-exposure, and the `get_template`/`send_submission` payload-translation approach grounded in the official guide. *Acceptance:* approval recorded in the ledger.
2. **Task-list approval (user checkpoint 2).** Critic/Saved-Task-List review of this revision, then final task approval. *Acceptance:* reviews PASS and approval recorded; gate reopens only after this.
3. **Tests first (RED).** Extend `tests/test_docuseal_mcp.py` and routing tests (`test_server_runtime.py`, `test_mcp_e2e.py`): origin-to-`/mcp` derivation and invalid-origin rejection, per-tool native invocation/input mapping/result decoding, removed-tool errors, startup/render with bearer unset, absence of REST requests/`X-Auth-Token`, token redaction, cancellation, and `send_submission` unknown outcome after dispatch without retry. *Acceptance:* new tests fail for the right reason.
4. **Implement (GREEN).** Extend `docuseal_mcp.py`; re-route all retained tools in `server.py`; delete `docuseal_api.py` and all removed-variable references (`.env.example`, `docker-compose.yml`, README, `SERVER_INSTRUCTIONS`, startup log). *Acceptance:* RED tests GREEN; no REST client code remains; `render_template_documents` untouched.
5. **Docs and configuration.** Update `.env.example` (two DocuSeal values), `docker-compose.yml` (drop `DOCUSEAL_API_KEY`; remove `DOCUSEAL_MCP_URL`), README (two-var contract, tool changes, removal rationale). *Acceptance:* docs match shipped behavior; no secrets in examples.
6. **Verification and reviews.** Focused tests, then full suite with ≥90% coverage and mocked bridge MCP smoke covering every retained DocuSeal tool. Code, Python, and security reviews (secret handling mandatory). No live upstream request, Docker build, deployment, or registry push without separate authorization. *Acceptance:* suite green at ≥90% coverage; reviews PASS; no live contact.
7. **Commit and push** after approval. *Acceptance:* conventional commit; ledger updated.

## 7. Acceptance Criteria (observable)

- **AC-R2-001 — Two-variable configuration:** The bridge starts and serves all tools with only `DOCUSEAL_URL` and `DOCUSEAL_MCP_BEARER_TOKEN` set. Active application/runtime configuration defines only those two DocuSeal values; `.env.example`, Compose, and README expose only them. Historical SpecDD text may name retired variables to document the cutover. Verify with targeted source/config searches and a mocked startup with exactly the two values.
- **AC-R2-002 — Derived endpoint:** The MCP endpoint is `{DOCUSEAL_URL}/mcp`; malformed/unsafe origins (HTTP non-loopback, credentials, query, fragment) fail at startup with a safe error. Verify with mocked client tests over invalid-origin fixtures.
- **AC-R2-003 — MCP-only routing:** Each retained DocuSeal tool calls exactly its documented native tool (`search_templates`, `load_template`, `send_documents`, `search_documents`) with Bearer auth and documented inputs; no request carries `X-Auth-Token`. Verify in mocks asserting tool name, arguments, and header per call.
- **AC-R2-004 — Unsupported behavior removed:** `get_submission_status` and `list_submissions` are absent; `search_submissions(q, limit)` replaces listing without template-ID/status parameters; removed names are not advertised. Verify via MCP tool discovery and tool-call tests.
- **AC-R2-005 — No new write tools:** `create_template` is not exposed; `send_submission` remains the only bridge tool that can send email. Verify via tool discovery.
- **AC-R2-006 — Ambiguous send outcome:** If `send_documents` was dispatched and the connection times out before a result, return a safe outcome-unknown error, do not retry, and instruct the caller to inspect DocuSeal before resending. Verify with a mocked post-dispatch disconnect and assert one upstream call.
- **AC-R2-007 — Failure isolation and redaction:** Missing token affects only DocuSeal-backed tools; rendering and startup stay healthy; token and upstream bodies never appear in errors, logs, or results. Verify with redaction and startup-isolation tests.
- **AC-R2-008 — Regression:** Offline export behavior unchanged; full suite ≥90% coverage; no live DocuSeal request occurred.

## 8. Approval Ledger

### Revision 2 (this document)

- Requirements/scope: **PENDING user approval** — exactly two DocuSeal env values (`DOCUSEAL_URL`, `DOCUSEAL_MCP_BEARER_TOKEN`); MCP-only operations; Option A removals; no `create_template`.
- Design/tasks: **PENDING user approval.**
- Implementation gate: **CLOSED/RESET on 2026-10-01.** Reopens only after checkpoints 1–2 above are recorded.

### Revision 1 records — SUPERSEDED 2026-10-01

- Requirements, Design, and Tasks approvals of 2026-09-30 covered search-only scope with four remaining REST tools and four env values. **Superseded** by this revision; they do not authorize Revision-2 work.
- Critic reviews (SpecDD, task-list) of 2026-09-30 applied to Revision 1 only; **reset** for Revision 2.
- The Revision-1 implementation gate (opened 2026-09-30) is **closed by this revision**; no Revision-2 implementation is authorized until the new approvals land.

## 9. References

- Official DocuSeal self-hosted MCP guide (tools: `search_templates`, `load_template`, `create_template`, `send_documents`, `search_documents`; auth `Authorization: Bearer`; endpoint `<origin>/mcp`): https://www.docuseal.com/guides/integrate-with-ai-agents-using-mcp
- DocuSeal REST API reference (legacy basis for removed behavior): https://www.docuseal.com/docs/api?lang=python
- Python MCP SDK v1.30.0 (selected pin): https://github.com/modelcontextprotocol/python-sdk/releases/tag/v1.30.0
