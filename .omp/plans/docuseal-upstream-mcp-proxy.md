# SpecDD: Proxy DocuSeal Native MCP Through the Bridge

**Status:** Revision 2 requirements/design approved; task-list amendment pending critic review and user re-approval. Test-only changes have begun but no tests have run; no application implementation or live DocuSeal contact has occurred.
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
| `get_template(template_id)` | REST `_api_client.get_template` | `load_template(template_id)` | **Re-route to native MCP.** Keep input name/type. On success, return the standard native-result envelope `{content: [...], structuredContent?: ...}` using JSON-safe MCP result fields; do not rebuild the REST template record or invent fields. |
| `list_submissions(template_id?, status?, query?, limit)` | REST `_api_client.list_submissions` | `search_documents(q, limit)` | **Replace with native-named `search_documents(q, limit)`.** No template-ID/status filters; result uses the native-result envelope. See §4. |
| `send_submission(template_id, signer_email, signer_name, signer_role, field_values?)` | REST `_api_client.send_submission` | `send_documents(template_id, submitters)` | **Re-route to native MCP.** Build `submitters=[{"email": signer_email, "name": signer_name, "role": signer_role}]`; when `field_values is not None`, add `fields=[{"name": name, "value": value} for name, value in field_values.items()]` **inside `submitters[0]`**, without coercing values. Return the native-result envelope. The operation may send a real email. If timeout/transport loss occurs after dispatch, return a safe outcome-unknown error, never retry automatically, and tell the caller to inspect DocuSeal before resending. |
| `get_submission_status(submission_id)` | REST `_api_client.get_submission_status` | none documented | **Removed.** No native tool or documented parameter fetches a submission by ID. |
| `render_template_documents` | local/offline | n/a | **Unchanged.** No DocuSeal credential, works with both new values unset. |

Not exposed: `create_template` (upstream write tool, no user approval).

## 4. Unsupported Semantics — Analysis and Recommendation

Two REST-backed capabilities lack a documented native MCP equivalent:

1. **Filtered submission listing.** `list_submissions` accepts `template_id`, `status`, and `query` filters. Native `search_documents` documents only `q` (free text) and `limit`. There is no documented way to filter by template ID or status upstream.
2. **Submission status by ID.** `get_submission_status(submission_id)` fetches a specific submission. No documented native tool or parameter performs a by-ID submission lookup.

### Options

- **Option A — remove unsupported tools; use documented MCP names/shapes (RECOMMENDED).** Remove `list_submissions` and `get_submission_status`; expose native `search_documents(q, limit)` without template-ID/status filters. Keep `get_template` and `send_submission` names/inputs, but return their native MCP success envelope instead of fabricating the old REST response shape. LibreChat callers must accept the removed tools and output-shape change.
- **Option B — keep REST-backed behavior and `DOCUSEAL_API_KEY`.** Violates the exactly-two-value MCP-only direction; rejected.
- **Option C — keep old filters/by-ID status by encoding them as free-text `q`.** Unsupported and misleading; rejected.
- **Option D — leave removed operations as no-ops or stubs.** Violates clean cutover; rejected.

### Why Option A is conservative

- It preserves every documented native capability needed by the retained bridge surface and avoids inventing data. It explicitly removes filter/by-ID behavior that native MCP does not document.

### Caller-visible behavior changes (Option A)

- `list_submissions` is replaced by `search_documents(q, limit)`; no `template_id` or `status` filters.
- `get_submission_status(submission_id)` disappears; no by-ID substitute is documented.
- `get_template` and `send_submission` keep their names and inputs but return native MCP response envelopes, not REST-specific objects.
- `send_submission` retains the existing real-email side effect; ambiguous post-dispatch outcomes are not retried automatically.

## 5. Design (delta from Revision 1)

- `docuseal_mcp.py` calls documented native MCP tools. `search_templates` returns `{"data":[{"id","name"}]}`. Successful `load_template`, `search_documents`, and `send_documents` calls return `{"content":[JSON-safe MCP content blocks], "structuredContent": <JSON object if supplied>}`; omit `structuredContent` when absent. Preserve native result content without reshaping to REST records. For `isError`, return a bounded generic bridge error and discard raw upstream error content.
- `server.py` exposes the retained tools `search_templates`, `get_template`, `send_submission`, and `search_documents`; routes each through `_mcp_client`; removes `_api_client` and REST credential globals. `get_submission_status` and `list_submissions` are removed.
- Configuration: when `DOCUSEAL_URL` is unset, use the documented default origin `https://signature.bullprog.com`; require a root origin (optional trailing slash only), derive `/mcp`, and validate HTTPS except loopback HTTP, no credentials/query/fragment. If a configured value is invalid, fail startup safely. An absent Bearer token does not block startup or local rendering; native MCP tool calls fail safely.
- Validate template/submission IDs as positive integers and limits as 1–100 before upstream calls. Map the one-signer `send_submission` input exactly to the documented `send_documents` submitter schema.
- Native MCP tool errors return bounded, redacted bridge errors. A timeout/transport loss after `send_documents` dispatch returns outcome-unknown; never retry; tell the caller to inspect DocuSeal before resending.
- SDK pin remains `mcp==1.30.0` (v1 line, protocol `2025-11-25`).
- `.env.example`, `docker-compose.yml`, and README expose only `DOCUSEAL_URL` and `DOCUSEAL_MCP_BEARER_TOKEN`; README states the removed submission filters/status lookup and native result envelopes.

## 6. Implementation Task List

1. **Requirements approval (user checkpoint 1).** Confirm exactly two DocuSeal env values, native-MCP-only routing, Option A compatibility losses, and no `create_template`. *Acceptance:* approval recorded.
2. **Design approval (user checkpoint 2).** Confirm retained tool names/result envelopes, exact `send_submission` to `send_documents` mapping, origin derivation, authentication behavior, redaction, and outcome-unknown handling. *Acceptance:* approval recorded.
3. **Task-list approval (user checkpoint 3).** Critic reviews the saved task list; present the exact implementation sequence and obtain approval. *Acceptance:* critic PASS and approval recorded; implementation gate opens only after all three user approvals and required critic verdicts are recorded.
4. **Tests first (RED).** Extend `tests/test_docuseal_mcp.py` and routing tests for URL derivation/validation, four retained tool mappings and result contracts, exact `send_documents` argument tree (including populated `submitters[0].fields` and omission when `field_values is None`), removed-tool discovery, startup/render with Bearer unset, no REST or `X-Auth-Token`, secret redaction, and ambiguous post-dispatch send failure with no retry. Delete REST-only `tests/test_docuseal_api.py`; preserve relevant behavior only where the native MCP contract supports it. *Acceptance:* expected assertions fail before implementation.
5. **Implement (GREEN).** Extend `docuseal_mcp.py`; route retained tools in `server.py`; update `Dockerfile` to copy `docuseal_mcp.py` and remove `docuseal_api.py`; delete `docuseal_api.py`, `tests/test_docuseal_api.py`, and active references to `DOCUSEAL_API_KEY`/`DOCUSEAL_MCP_URL` (`.env.example`, `docker-compose.yml`, README, startup log/instructions). *Acceptance:* tests GREEN; no REST client remains; container includes its native MCP adapter; renderer unchanged.
6. **Docs and configuration.** Update `.env.example`, Compose and README for exactly two active DocuSeal values and the changed tool/result contracts. *Acceptance:* configs/docs expose only the two approved values.
7. **Verification and reviews.** Run focused tests, full suite with ≥90% coverage, and mocked bridge MCP smoke for all retained tools. Complete code, Python and security reviews. No live upstream request, Docker build, deployment, or registry push. *Acceptance:* suite green at ≥90%; reviews PASS; no live contact.
8. **Commit/push gate.** Present final diff and proposed commit message; commit/push only after user approval. *Acceptance:* conventional commit and ledger updated.

## 7. Acceptance Criteria (observable)

- **AC-R2-001 — Two-variable configuration:** With `DOCUSEAL_URL` unset, the documented default applies; with `DOCUSEAL_MCP_BEARER_TOKEN` unset, bridge startup and local rendering remain available while MCP calls fail safely. Active runtime/Compose/README config exposes only those two values; historical plan text may name retired variables. Verify default, missing-token, and configured startup separately.
- **AC-R2-002 — Derived endpoint:** Derive exactly `{normalized DOCUSEAL_URL}/mcp`; accept only HTTPS remote root origins or loopback HTTP root origins, with optional trailing slash. Reject invalid configured origins at startup. Verify safe/unsafe fixtures.
- **AC-R2-003 — MCP-only routing:** Each retained tool calls its documented upstream MCP operation with Bearer auth, validated arguments, and no REST request/header. Verify upstream call name/arguments and auth in mocks.
- **AC-R2-004 — Unsupported behavior removed:** `list_submissions` and `get_submission_status` are absent; `search_documents(q, limit)` provides only the documented free-text search. Verify tool discovery and that removed names are not advertised.
- **AC-R2-005 — Native result contracts:** `search_templates` returns `{"data":[{"id","name"}]}`; other retained tools return JSON-safe native `{content, structuredContent?}` success envelopes and never fabricate REST fields. `isError` results become safe redacted errors. Verify each with deterministic result fixtures.
- **AC-R2-006 — No new write tools:** `create_template` is not exposed; `send_submission` is the only bridge tool that can email signers. Verify tool discovery.
- **AC-R2-007 — Ambiguous send outcome:** If `send_documents` was dispatched and its response is lost, return outcome-unknown, make one attempt only, and direct the caller to inspect DocuSeal before resending. Verify a mocked post-dispatch disconnect.
- **AC-R2-008 — Failure isolation/security:** Missing Bearer token does not block startup/rendering; token/upstream error bodies never leak to logs or responses. Verify missing-token and redaction paths.
- **AC-R2-009 — Regression:** Offline export remains unchanged; full suite ≥90% coverage; no live DocuSeal request.

## 8. Approval Ledger

### Revision 2 (this document)

- Requirements/scope: **APPROVED by user on 2026-10-01** — exactly two DocuSeal env values (`DOCUSEAL_URL`, `DOCUSEAL_MCP_BEARER_TOKEN`); MCP-only operations; Option A removals; no `create_template`.
- Design: **APPROVED by user on 2026-10-01** — server-side Bearer MCP adapter, root-origin `/mcp` derivation, native result envelopes, exact submitter-field mapping, and safe handling of missing credentials and ambiguous sends.
- Tasks: **APPROVED by user on 2026-10-01** — RED-first tests, native MCP migration and REST/config removal, Dockerfile adapter packaging, docs, focused/full verification and reviews; no live DocuSeal calls or deployment.
- Critic reviews: prior **PASS (2026-10-01)** covered the earlier task list. The Dockerfile packaging amendment also received **Critic PASS (2026-10-01; no findings)**. Previous result-contract, URL/token, and nested-field findings remain resolved.
- Implementation gate: **OPEN on 2026-10-01.** Requirements, design, amended task approval, and Critic PASS are recorded.
- Implementation: **COMPLETE and pushed (2026-10-01), `52a91e08fdfaa76156417eead6202e92f94c5625`.** Native MCP-only routes, two-value configuration, unsupported-operation removal, docs, and Docker packaging are implemented. The post-dispatch cancellation regression failed before the fix and passed on the same test after it.
- Verification: focused suite **86 passed**; full suite **185 passed, 2 skipped, 90.11% coverage** (90% required); in-process Streamable HTTP smoke covers all retained upstream MCP operations. No live DocuSeal request, Docker build, deployment, or registry push.
- Reviews: final code, Python, and security reviews **PASS**. Review findings on ambiguous send results and post-dispatch cancellation were fixed and re-reviewed.
- Commit/push gate: **APPROVED by user on 2026-10-01** after review of the final diff; implementation commit `52a91e08fdfaa76156417eead6202e92f94c5625` was pushed to `origin/main`.

### Revision 1 records — SUPERSEDED 2026-10-01

- Requirements, Design, and Tasks approvals of 2026-09-30 covered search-only scope with four remaining REST tools and four env values. **Superseded** by this revision; they do not authorize Revision-2 work.
- Critic reviews (SpecDD, task-list) of 2026-09-30 applied to Revision 1 only; **reset** for Revision 2.
- The Revision-1 implementation gate (opened 2026-09-30) is **closed by this revision**; no Revision-2 implementation is authorized until the new approvals land.

## 9. References

- Official DocuSeal self-hosted MCP guide (tools: `search_templates`, `load_template`, `create_template`, `send_documents`, `search_documents`; auth `Authorization: Bearer`; endpoint `<origin>/mcp`): https://www.docuseal.com/guides/integrate-with-ai-agents-using-mcp
- DocuSeal REST API reference (legacy basis for removed behavior): https://www.docuseal.com/docs/api?lang=python
- Python MCP SDK v1.30.0 (selected pin): https://github.com/modelcontextprotocol/python-sdk/releases/tag/v1.30.0
