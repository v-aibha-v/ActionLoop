# ActionLoop

**Turn a meeting transcript into reviewable action items — and put a human approval gate between the AI and your calendar.**

ActionLoop is a FastAPI backend plus a small review UI that reads an unstructured meeting transcript, asks Claude to extract structured action items, validates them with Pydantic, and then waits. Nothing is written to Google Calendar or Gmail until a person approves an item. Approved items are executed into a Calendar event and a Gmail **draft**.

```
Meeting transcript -> Claude API -> structured items -> Pydantic validation
    -> HUMAN REVIEW (edit / approve / reject) -> approved item -> executor
                                                            |-> Google Calendar event
                                                            |-> Gmail draft (never sent)
```

---

## The problem

Meetings produce commitments in a format no tool can act on. Someone says *"I'll get the benchmark report out by the 25th"*, and that promise lives in a paragraph of prose. The usual responses are both unsatisfying:

- **Manual triage.** A person re-reads the transcript and types tasks into a tracker. It is the most reliable option and the first thing to be dropped when the week gets busy.
- **Fully automated extraction.** An LLM dumps tasks straight into a task manager. Now a model that occasionally hallucinates can create calendar invites and email drafts in your real account, and nobody approved the destination.

## The solution

ActionLoop keeps the useful half of automation and refuses the dangerous half. The LLM does the reading — that is genuinely hard and genuinely useful — and the human keeps the *authority*. Approval is enforced in the service layer, so it holds for the UI, for `curl`, and for any future client:

- Extraction has **no** external side effects. It writes to a local database and stops.
- Execution refuses anything that is not `approved`. A rejected item is permanently inert.
- Gmail is **drafts only**. The app requests the `gmail.compose` scope, not `gmail.send`, so it *structurally cannot* send mail.

## Features

| Area | What it does |
| --- | --- |
| **Extraction** | Claude is called with a forced tool-use schema, so the response is structured JSON rather than prose that needs parsing. |
| **Validation** | Two Pydantic models: a tolerant one for the LLM contract, a strict one for the domain entity. Malformed output is rejected, never silently stored. |
| **Review** | List, inspect, edit, approve, reject. Edits are re-validated before they are persisted. |
| **Approval gate** | A declarative state machine (`extracted -> approved -> executed`, `rejected` terminal) enforced server-side on every request. |
| **Execution** | One approved item becomes one Calendar event and one Gmail draft. Failures are recorded per-target and the item becomes `failed`, so it can be retried. |
| **Idempotency** | Four layers of duplicate protection, so a double-click or a retry after a crash cannot create a second event or draft. |
| **Error handling** | Typed error hierarchy with stable machine-readable codes, a single error envelope, and credential redaction before anything is logged. |
| **Demo UI** | A dependency-free page: paste a transcript, extract, review, approve, execute, see both results. |

## Architecture

```text
                       +-----------------------------+
   browser  ---------> |  app/static  (review UI)    |
                       +--------------+--------------+
                                      | HTTP (fetch)
                       +--------------v--------------+
                      |        app/api (FastAPI)     |
                      |  routes · schemas · auth     |
                      +--------------+---------------+
                                     | depends on
                       +-------------v---------------+
                       |    app/services  (rules)    |
                       |  extraction · approval ·    |
                       |  execution · calendar ·     |
                       |  gmail                      |
                       +--+----------+------------+--+
                          |          |            |
        +-----------------v--+  +----v-----+  +---v----------------+
        | app/agents         |  | app/db   |  | app/integrations   |
        | Claude extraction  |  | SQLAlch. |  | Google OAuth,      |
        | (tool use)         |  | repos    |  | Calendar, Gmail    |
        +--------------------+  +----------+  +--------------------+
              |                     |                  |
        Anthropic API         SQLite file      Google Calendar / Gmail
```

The dependency direction is one-way. Routes know services, services know repositories and integrations, and nothing points back up. That is what makes the approval gate testable at the service level rather than only through HTTP.

## Tech stack

- **Python 3.12+**, type-annotated throughout
- **FastAPI** + **Uvicorn** — HTTP layer and OpenAPI docs
- **Pydantic v2** / **pydantic-settings** — validation and typed configuration
- **Anthropic SDK** — Claude with forced tool use for structured output
- **SQLAlchemy 2.0** — ORM over SQLite (swappable via `DATABASE_URL`)
- **Google API Python client** — Calendar and Gmail behind narrow adapters
- **pytest** + **pytest-asyncio** — 67 tests, fully offline
- **Ruff** — linting and import ordering

## Project structure

```text
actionloop/
├── app/
│   ├── main.py                  # app factory, request middleware, error handlers
│   ├── api/
│   │   ├── routes.py            # health, transcripts, extraction, review, execute
│   │   ├── auth_routes.py       # Google OAuth start / callback / status / disconnect
│   │   ├── schemas.py           # request + response models
│   │   └── dependencies.py      # the container dependency
│   ├── agents/
│   │   ├── extraction_agent.py  # transcript -> validated action items
│   │   └── prompts.py           # system prompt + forced tool JSON schema
│   ├── core/
│   │   ├── config.py            # Settings, OAuth scopes
│   │   ├── container.py         # composition root (the one place things are wired)
│   │   ├── exceptions.py        # error hierarchy + credential redaction
│   │   ├── logging.py           # structured logging, request ids
│   │   └── examples.py          # bundled sample transcript
│   ├── db/
│   │   ├── database.py          # engine + session factory
│   │   ├── models.py            # SQLAlchemy tables
│   │   └── repository.py        # mappers + repositories
│   ├── integrations/
│   │   ├── google_auth.py       # OAuth 2.0, token cache
│   │   ├── google_calendar.py   # Calendar adapter
│   │   ├── google_gmail.py      # Gmail adapter (drafts only)
│   │   └── http_errors.py       # Google HttpError -> domain error
│   ├── models/
│   │   ├── action_item.py       # LLM contract, domain entity, state machine
│   │   ├── execution.py         # per-target operation outcomes
│   │   └── transcript.py        # transcript entity
│   ├── services/
│   │   ├── extraction_service.py
│   │   ├── approval_service.py  # the human-in-the-loop gate
│   │   ├── execution_service.py # the only code path with side effects
│   │   ├── calendar_service.py
│   │   └── gmail_service.py
│   └── static/                  # index.html, app.js, styles.css
├── tests/
│   ├── conftest.py              # fake Claude/Google clients, in-memory database
│   ├── test_extraction.py       # prompt contract, parsing, failure modes
│   ├── test_review_workflow.py  # extraction -> edit -> approve/reject over HTTP
│   ├── test_execution.py        # approval gate, idempotency, provider failures
│   └── test_google_auth.py      # OAuth status surface
├── examples/
│   ├── sample_transcript.txt    # a realistic transcript with traps
│   └── api-responses.json       # example API payloads
├── docs/
│   ├── architecture.md          # invariants, idempotency, failure policy
│   ├── google-oauth-setup.md    # Google Cloud Console walkthrough
│   └── demo.md                  # five-minute demo script
├── requirements.txt
├── pyproject.toml               # package metadata, pytest + ruff config
├── .env.example
└── .gitignore
```

## Setup

```bash
git clone https://github.com/v-aibha-v/ActionLoop.git
cd ActionLoop

python -m venv .venv
.venv\Scripts\Activate.ps1        # Windows PowerShell
# source .venv/bin/activate       # macOS / Linux

pip install -r requirements.txt
```

Then create your local configuration file — it is git-ignored, and it is the only place secrets belong:

```bash
cp .env.example .env               # Windows: Copy-Item .env.example .env
```

The application starts without any credentials. It will report honestly which integrations are usable:

```bash
curl http://localhost:8000/health
# {"status":"ok","llm_configured":false,"google_configured":false,...}
```

Claude is required for extraction; Google is only required at the execution step. So you can run and review the whole pipeline with just an Anthropic key, and add Google later.

## Environment variables

Everything is optional except the credentials for the integration you actually use. Values are validated at startup.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | For extraction | — | Claude API key. Never logged; redacted if it ever appears in an error. |
| `ANTHROPIC_MODEL` | No | `claude-sonnet-4-5` | Model id your account can access. |
| `ANTHROPIC_MAX_TOKENS` | No | `4096` | Response cap for the extraction call. |
| `ANTHROPIC_TIMEOUT_SECONDS` | No | `60` | Per-request timeout. |
| `DATABASE_URL` | No | `sqlite:///./actionloop.db` | Any SQLAlchemy URL. SQLite by default. |
| `ENVIRONMENT` | No | `development` | Reported by `/health`. |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`. |
| `GOOGLE_CLIENT_ID` | For execution | — | OAuth 2.0 client id. |
| `GOOGLE_CLIENT_SECRET` | For execution | — | OAuth 2.0 client secret. |
| `GOOGLE_REDIRECT_URI` | For execution | `http://localhost:8000/auth/google/callback` | Must match the URI registered in Google Cloud. |
| `GOOGLE_TOKEN_PATH` | No | `.secrets/google_token.json` | Cached refresh token. Lives under git-ignored `.secrets/`. |
| `GOOGLE_CALENDAR_ID` | No | `primary` | Target calendar. |
| `GOOGLE_CALENDAR_TIMEZONE` | No | `UTC` | Timezone for created events. |
| `CALENDAR_EVENT_DURATION_MINUTES` | No | `30` | Length of the created event. |
| `GMAIL_FOLLOW_UP_RECIPIENT` | No | empty | Draft recipient. If empty the draft has no `To` header, and ActionLoop never guesses an address. |
| `MAX_TRANSCRIPT_CHARS` | No | `40000` | Rejects oversized transcripts before calling Claude. |
| `MAX_EXTRACTED_ITEMS` | No | `50` | Cap on items per transcript, imposed on the tool schema too. |

## Google OAuth setup

Full walkthrough with screenshots-in-words in [`docs/google-oauth-setup.md`](docs/google-oauth-setup.md). The short version:

1. Create a project in the [Google Cloud Console](https://console.cloud.google.com/).
2. **APIs & Services -> Library**: enable **Google Calendar API** and **Gmail API**.
3. **OAuth consent screen**: choose *External*, add your own address as a test user.
4. **Credentials -> Create credentials -> OAuth client ID -> Web application**, with the authorised redirect URI
   `http://localhost:8000/auth/google/callback` (it must match `GOOGLE_REDIRECT_URI` exactly).
5. Put the client id and secret in `.env`.
6. Start the app and click **Connect Google**, or open `http://localhost:8000/auth/google`.
7. Confirm with `GET /auth/google/status` — it reports `configured`, `authenticated`, the requested `scopes`, and the signed-in `connected_email`.

Only two scopes are ever requested:

| Scope | Why |
| --- | --- |
| `.../auth/calendar.events` | Create the event for an approved item. |
| `.../auth/gmail.compose` | Create the follow-up **draft**. |

`gmail.send` is deliberately **not** requested. The application cannot send mail even if a bug tried to.

## How to run

```bash
uvicorn app.main:create_app --factory --reload
```

| URL | What it is |
| --- | --- |
| <http://localhost:8000> | The review UI |
| <http://localhost:8000/docs> | Generated OpenAPI documentation |
| <http://localhost:8000/health> | Liveness + configuration report |

The factory form matters: importing `app.main` has no side effects, and no database file is created until the server actually starts.

### API reference

| Method | Path | Purpose | Notable responses |
| --- | --- | --- | --- |
| `GET` | `/health` | Liveness and which integrations are configured | `200` |
| `POST` | `/api/transcripts` | Store a transcript (no LLM call) | `201`, `422` |
| `GET` | `/api/transcripts/{id}` | Fetch a stored transcript | `200`, `404` |
| `POST` | `/api/action-items/extract` | Store + extract with Claude | `201`, `422`, `502` |
| `GET` | `/api/action-items` | List, filterable by `status` / `transcript_id` | `200` |
| `GET` | `/api/action-items/{id}` | Fetch one item | `200`, `404` |
| `PATCH` | `/api/action-items/{id}` | Edit during review | `200`, `404`, `409`, `422` |
| `POST` | `/api/action-items/{id}/approve` | Approve (the human gate) | `200`, `404`, `409` |
| `POST` | `/api/action-items/{id}/reject` | Reject — terminal | `200`, `404`, `409` |
| `POST` | `/api/action-items/{id}/execute` | Create the Calendar event + Gmail draft | `200`, `401`, `404`, `409`, `502` |
| `GET` | `/api/examples/transcript` | The bundled sample transcript | `200`, `404` |
| `GET` | `/auth/google` | Start the OAuth flow (returns the consent URL) | `200`, `502` |
| `GET` | `/auth/google/callback` | OAuth redirect target | `303`, `400`, `502` |
| `GET` | `/auth/google/status` | Configured / authenticated / scopes / account | `200` |
| `DELETE` | `/auth/google` | Forget the cached token | `200` |

Every failure uses one envelope, with a stable machine-readable code:

```json
{"error": {"code": "execution_not_allowed",
           "message": "Cannot execute an extracted action item.",
           "details": {"status": "extracted"}}}
```

## How to test

```bash
python -m pytest -q      # 67 tests, no network, no credentials

# Optional linting. Ruff is configured in pyproject.toml but is not a runtime
# or test dependency, so install it separately if you want to run it.
pip install ruff
ruff check .
```

The suite never calls Claude or Google. Three seams are substituted (the Anthropic client, the Calendar client, the Gmail client), so the tests exercise the real routes, services, state machine and repositories against an in-memory SQLite database — only the outermost I/O is fake. `tests/conftest.py` documents that design.

## Example transcript

`examples/sample_transcript.txt` is a synthetic Q3 review, written with deliberate traps so the extraction step has something to get *wrong*:

```text
Vaibhav: Yes - let's make the call now: we will go with the managed Postgres
offering for the new pipeline. That is a decision, not a task, so someone should
still write it down in the design doc.

Dana: I can put the decision in the design doc.
...
Priya: Yes. I will take it and get it done by next Friday.
...
Marcus: Also, honestly, I am not sure the cost numbers in the appendix are
right. Someone should probably double-check them at some point.
```

It contains a **decision** (not a task), a request with **no date**, a task with **no owner**, an explicit **"I will"** commitment with a relative deadline, and a **vague aside**. A good extraction keeps the commitments, does not invent a date or an owner it was never given, and records the verbatim context so a reviewer can judge it.

## Example extracted action items

Claude returns this shape (forced tool call, so it is always structured):

```json
[
  {
    "title": "Upgrade the staging cluster to Postgres 16",
    "description": "Staging runs Postgres 13, so the new query planner cannot be tested there.",
    "owner": "Priya",
    "deadline": "2026-09-25T00:00:00Z",
    "priority": "high",
    "source_context": "I will take it and get it done by next Friday.",
    "confidence": 0.92
  },
  {
    "title": "Prepare the benchmark report",
    "description": "Write up the Q3 database benchmark findings for the customer calls context.",
    "owner": "Vaibhav",
    "deadline": "2026-09-25T17:00:00Z",
    "priority": "high",
    "source_context": "I will prepare the benchmark report and I will have it ready by September 25.",
    "confidence": 0.95
  },
  {
    "title": "Write the Postgres decision into the design doc",
    "description": "Record the decision to use managed Postgres for the new pipeline.",
    "owner": "Dana",
    "deadline": null,
    "priority": "medium",
    "source_context": "I can put the decision in the design doc.",
    "confidence": 0.88
  }
]
```

Note the third item: `deadline` is `null` because the meeting never gave one. The prompt forbids inventing a plausible-looking date, so the model reports lower confidence instead of guessing. `examples/api-responses.json` shows the persisted, post-execution shape.

## The human-in-the-loop gate

Approval is not a UI convention here — it is an application rule with one implementation:

```text
extracted ──approve──> approved ──execute──> executed (terminal)
    │                       │
    └──reject──> rejected   └──provider failure──> failed ──retry──> executed
                (terminal)                              └──reject──> rejected
```

What that buys you:

- `POST /execute` on an `extracted` or `rejected` item returns `409 execution_not_allowed`. The check lives in `ExecutionService._ensure_executable`, so calling the API directly, scripting it with `curl`, or hiding the button all behave identically.
- Editing is only allowed while an item is `extracted` or `failed`. Once approved, the reviewed content is frozen — otherwise the thing that gets executed would not be the thing that was approved.
- Re-approving a `failed` item is refused; the correct move is to retry execution, which reuses the resources already created.
- Every transition is validated against a declarative `ALLOWED_TRANSITIONS` table, so no service can invent a shortcut and the edges can be unit tested exhaustively.

## Security considerations

| Concern | How it is handled |
| --- | --- |
| Secrets in version control | `.env`, `.secrets/`, `credentials.json`, `client_secret*.json`, `*token*.json` and `*.pem`/`*.key` are all git-ignored, and a test asserts the rules stay in place. |
| Credential leakage through errors | `redact()` scrubs Anthropic keys, Google API keys, OAuth secrets and access/refresh tokens before an error is logged or returned. Provider SDK errors happily quote the `Authorization` header they sent, so this is not theoretical. |
| Excessive OAuth permissions | Only `calendar.events` and `gmail.compose`. No `gmail.send`, no full-mailbox access. |
| Sending mail by accident | There is no send method in the Gmail client, no send call anywhere in `app/`, and a test scans the package to keep it that way. |
| Acting without consent | Execution requires `approved` status, enforced server-side. Rejected items are inert forever. |
| Duplicate side effects | Four idempotency layers, ending in a compare-and-set database write. |
| Unbounded or hostile input | Transcripts are length-capped and item counts capped. The prompt marks the transcript as untrusted data and instructs the model to treat instructions inside it as text to report, not commands to obey. |
| Internal detail in responses | An unhandled exception returns a generic `500` envelope; the traceback goes to the log, never to the client. |

**Deliberate limitation, stated plainly:** this is a single-user local tool. The API has no authentication and no multi-tenancy — SQLite holds one person's data and the OAuth token belongs to whoever completed the consent flow. Exposing it as a shared service would require API auth, per-user token storage and per-user data scoping. Skipping that is a scope decision, not an oversight.

## Design decisions

| Decision | Why |
| --- | --- |
| Two Pydantic models: a tolerant LLM contract and a strict domain entity | An LLM will always produce `"2026-09-25"` where you wanted a datetime, or `null` where you wanted a string. Normalising at the boundary keeps that mess out of the domain and lets the contract evolve without touching the entity. |
| Forced tool use instead of "reply with JSON" | The response shape is guaranteed by the API contract, so there is no brittle text parsing to break on a stray code fence. |
| The state machine is data, not control flow | `ALLOWED_TRANSITIONS` can be inspected, tested and rendered. Adding a state is a one-line change with no scattered `if status ==` checks to miss. |
| No database transaction held across a network call | Doing so pins a connection, blocks SQLite readers and turns a slow third party into an outage. The transcript is read, Claude is called, then results are written in a second short transaction. |
| A container as the composition root | Claude and Google are exactly the things tests must replace. One object with three injectable seams means the whole app can be built offline in three lines. |
| Google clients resolved through a callable, not an instance | A token refreshed mid-session is picked up automatically, and tests inject a fake without touching OAuth. |
| Provider failures return outcomes, not exceptions | A Calendar outage must not discard the Gmail draft that already succeeded. Each target reports its own result, and the item's final status summarises them. |
| Compare-and-set write for the final status | The external call cannot live inside the transaction, so the write is conditional on the status that was read. A double-clicked Execute updates zero rows and gets a `409` instead of recording two executions. |
| Idempotency by searching, not only by remembering | A crash *after* Google succeeded but *before* the local write is precisely the case local bookkeeping misses, so each client first looks for the resource it would have created. |
| Gmail idempotency uses a body marker | Gmail search does not index arbitrary headers, so `AL-REF-<action item id>` in the draft body is the only reliable way to find a previously created draft. |
| Strict, explicit confidence validation | A bare `90` is rejected rather than silently rescaled to `0.9`: "0-100 percent", "0-10 score" and "typo for 0.9" are equally plausible, and guessing hides a prompt regression. An explicit `"90%"` is rescaled. |
| No frontend framework | The interesting engineering is the pipeline and the gate. A build toolchain would add moving parts without changing what a reviewer can do. |
| Blocking routes declared with `def` | Database work and the Google client are blocking calls; FastAPI runs them in a threadpool instead of stalling the event loop. Only extraction is `async`, because the Anthropic client genuinely is. |

## Demo workflow

A five-minute run, also written up in [`docs/demo.md`](docs/demo.md):

1. `uvicorn app.main:create_app --factory --reload`, then open <http://localhost:8000>.
2. Press **Load sample transcript** — the Q3 review, with its traps.
3. Press **Extract action items**. Items appear as `Extracted — awaiting your decision`. Nothing has touched Google yet.
4. Edit one: fix a title, assign the unowned client summary to yourself, set a deadline. The edit is re-validated before it is saved.
5. Reject the vague one ("double-check the cost numbers") to demonstrate `Rejected — will never be executed`.
6. Press **Execute** on a still-extracted item — the server refuses it with `409`. That is the gate.
7. Approve an item that has a deadline, then **Execute** it. You get a Calendar event link and a Gmail draft id, and the card shows `Executed` with **"Draft created — not sent."**
8. Press **Execute** again — `409 already_executed`, and nothing new appears in Google.
9. Open Gmail: the draft is there. It is still a draft.

## Future extensions

Designed to extend to other productivity tools such as Slack, Notion, and task-management platforms. Those integrations are **not implemented** — only Google Calendar and Gmail are, and Gmail only as drafts. The seams that would make them straightforward already exist:

- A new destination is a client implementing the same narrow protocol, plus a service returning `OperationOutcome`. The execution service, the idempotency scheme and the approval gate are then reused unchanged.
- Slack and Notion would each need their own idempotency marker, exactly as Gmail uses a body marker and Calendar an extended property.
- Replacing `create_all` with Alembic migrations is the one change needed before a multi-environment deployment.
- A "re-extract this transcript" endpoint would be trivial: transcripts are stored as their own entity precisely so extraction can be re-run and compared.

## Project notes

- **Author:** Vaibhav ([@v-aibha-v](https://github.com/v-aibha-v))
- **Version:** 0.1.0
- **Tests:** 67 passing, entire suite offline

## Licence

No licence file is included yet, so all rights are reserved by the author. Add one before using this in a way that requires it.
