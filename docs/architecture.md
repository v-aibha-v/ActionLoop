# Architecture

## Layers

| Layer | Responsibility | Must not |
| --- | --- | --- |
| `app/api` | HTTP shape, status codes, request/response schemas | Contain business rules |
| `app/services` | Business rules — the approval gate, execution, idempotency | Know about HTTP or FastAPI |
| `app/agents` | Text in, validated structured data out | Persist, approve, or call Google |
| `app/models` | Pydantic contracts and the lifecycle state machine | Perform I/O |
| `app/db` | SQLAlchemy tables, mappers and repositories | Contain business rules |
| `app/integrations` | Google OAuth, Calendar and Gmail adapters; translate `HttpError` into domain errors | Contain business rules |
| `app/core` | Settings, logging, errors, the composition root | Depend on any layer above it |

Dependencies point one way only: `api -> services -> {models, db, integrations} -> core`. Nothing points back up, which is why the approval gate can be tested at the service level and the whole application can be built with three substituted seams.

## The invariants

These are the properties the project exists to guarantee. Each is enforced by code rather than convention, and each has at least one test that fails if it is broken.

1. **No external side effect happens before approval.** `ExecutionService._ensure_executable` refuses anything that is not `approved` (or `failed`, i.e. a retry). It is the only code path that calls Google.
2. **Gmail drafts only.** The requested scope is `gmail.compose`; `GmailClient` has no send method; no module in `app/` contains a `drafts().send` or `messages().send` call. A test scans the package to keep it that way.
3. **Rejection is terminal.** `ALLOWED_TRANSITIONS[REJECTED]` is empty, so a rejected item can never be approved or executed.
4. **Everything persisted has been validated.** Edits round-trip through `model_validate`, so a merged copy cannot carry an invalid value into the database.
5. **Credentials stay in `.env` and `.secrets/`.** Both are git-ignored, error text is redacted before it is logged, and `/health` and `/auth/google/status` report booleans and public identifiers only.

## Lifecycle

| From | Allowed to | Notes |
| --- | --- | --- |
| `extracted` | `approved`, `rejected` | Waiting for a human decision. Editable. |
| `approved` | `executed`, `failed` | Frozen: the reviewed content is what will be executed. Not editable. |
| `failed` | `executed`, `rejected` | The retry path. Editable, so a bad deadline can be corrected. |
| `executed` | — | Terminal. A repeat execution is refused rather than repeated. |
| `rejected` | — | Terminal and inert. |

The table lives in `app/models/action_item.py` as `ALLOWED_TRANSITIONS`, and every status change in the codebase is validated against `can_transition` before it is written.

## Idempotency, in four layers

A single approved item can be executed more than once — a double-click, a retry after a failure, or a crash mid-flight. Duplicates are prevented in this order:

1. **Status gate.** An already-`executed` item raises `AlreadyExecutedError` (`409`). Most repeats never get past this.
2. **Local record.** If the item already carries a `calendar_event_id` or `gmail_draft_id`, the operation is reported as `already_exists` with no API call at all.
3. **Remote search.** Before creating anything, the client asks Google whether a previous attempt already created it — Calendar by an extended property holding the action item id, Gmail by a quoted `AL-REF-<id>` marker in the draft body. This covers the crash *after* Google succeeded but *before* the local write, which layers 1 and 2 cannot see.
4. **Compare-and-set write.** The final status update includes the previously read status in its `WHERE` clause, so the check and the write are one atomic statement. If another request changed the row while the external calls were in flight, zero rows are updated and the caller gets a `409`.

Layer 4 matters because the external calls cannot happen inside a database transaction. Read-status, call-Google, write-status is inherently racy; making the write conditional moves the correctness guarantee into the single statement that finishes the operation.

## Failure policy

| Failure | Behaviour | What the client sees |
| --- | --- | --- |
| Claude not configured | `ConfigurationError`, raised before any request is made | `500 configuration_error` naming the missing variable, never its value |
| Claude transport/API error | Wrapped as `ExtractionError`, message redacted | `502 extraction_error` |
| Claude answers without the tool call, or with the wrong tool | `LLMResponseError` | `502 llm_response_error` |
| Tool payload fails validation | `ExtractionValidationError` with field-level detail | `502 extraction_validation_error` |
| Empty or oversized transcript | `EmptyTranscriptError` / `TranscriptTooLongError`; Claude is never called | `422` |
| Illegal status change | `InvalidStateTransitionError`, listing the statuses that *are* allowed | `409 invalid_state_transition` |
| Execute on an unapproved item | `ExecutionNotAllowedError` | `409 execution_not_allowed` |
| Execute an already-executed item | `AlreadyExecutedError` | `409 already_executed` |
| Google not connected / token expired | `NotAuthenticatedError`, raised *before* any item is marked failed | `401 google_not_authenticated` |
| Calendar API failure | A `failed` outcome for that target only | `200`, item `failed` with `last_error` set, retryable |
| Gmail API failure | Same, independently of Calendar | as above |
| Anything unexpected | Logged with a traceback, generic envelope returned | `500 internal_error`, no internal detail |

The asymmetry is deliberate. A **provider** failure belongs to the action item: the item becomes `failed`, whatever succeeded is preserved, and a retry resumes rather than restarts. A **session** failure (no Google credential) belongs to the user, so the item keeps its `approved` status and nothing is recorded as failed. A **bad input** failure never reaches the provider at all.

## Data model

```text
transcripts                    action_items
-----------                    ------------
id (uuid, pk)                  id (uuid, pk)
title                          transcript_id (fk -> transcripts.id, cascade)
content (text)                 title, description, owner
meeting_date                   deadline (utc), priority, source_context, confidence
created_at                     status (indexed)
                               calendar_event_id, calendar_event_link
                               gmail_draft_id, last_error
                               created_at, updated_at
                               approved_at, rejected_at, executed_at
```

A transcript is its own entity so re-extraction produces a fresh, comparable batch and every item stays traceable to the text that produced it. All timestamps are timezone-aware UTC, because comparing a naive datetime with an aware one raises `TypeError` and the bug surfaces at the worst possible moment.

## Request lifecycle

1. Middleware assigns a request id (or reuses an incoming `X-Request-ID`) and starts a timer.
2. The route resolves the container from `app.state`, which is how tests swap in fakes without monkeypatching globals.
3. The service opens a short session, loads the item, and validates the requested transition.
4. For an execution, the Google calls happen with **no transaction open**, using a credential resolved at that moment so a refreshed token is picked up.
5. Results are written back in a second short session, conditionally on the status that was read.
6. Middleware logs one structured line with the status code and duration, and echoes the request id.

## Extension points

- **A new destination** (Slack, Notion, a task manager): implement the narrow client protocol, write a service returning `OperationOutcome`, and add it to the executor's outcome list. The gate and the idempotency scheme are reused unchanged.
- **A different LLM**: `ExtractionAgent` is the only module that knows about Anthropic. Anything that returns the contracted tool payload works.
- **A different database**: set `DATABASE_URL`. Only `create_all` would need to become a migration.
- **A different UI**: the API is complete without it. The bundled page is a convenience, not a dependency.