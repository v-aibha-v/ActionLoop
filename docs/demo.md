# Demo script

A five-minute walkthrough. It is written as narration, so you can follow it live or hand it to someone else.

## What you need

- An `ANTHROPIC_API_KEY` in `.env` for steps 1-6.
- Google credentials for steps 7-9. Without them the demo still works up to the approval gate; execution returns `401` and the item keeps its `approved` status, which is itself a good thing to show.

```bash
pip install -r requirements.txt
uvicorn app.main:create_app --factory --reload
```

Open <http://localhost:8000>. Check the badge in the top right: it tells you whether Google is configured, connected, and — if connected — which account.

## 1. Load the transcript

Press **Load sample transcript**. It fills in the Q3 Benchmark Review, a synthetic meeting containing deliberate traps:

- a **decision** ("we will go with the managed Postgres offering") — not a task;
- a request with **no date** ("share the dashboard mockups... when you can");
- a task with **no owner** ("we need a short client-facing summary");
- an explicit commitment with a **relative** deadline ("get it done by next Friday");
- a **vague aside** ("someone should probably double-check the cost numbers").

> "This transcript is the hard case. A naive pipeline either drops the vague items or invents owners and dates for them."

## 2. Extract

Press **Extract action items**. Each item appears with the label `Extracted — awaiting your decision`.

> "Claude was called with a forced tool schema, so this is structured data, not text we parsed by hand. Every item is validated by Pydantic before it is stored. And note what has *not* happened: nothing has touched Google yet."

Point at an item's fields: owner, deadline, priority, confidence, and the verbatim `source_context` quote.

> "The quote is what makes review possible. You can judge the extraction against the sentence it came from without re-reading the whole transcript."

## 3. Edit one

Press **Edit** on an item, change the title, assign the unowned client summary to yourself, set a deadline, and save.

> "An edited value is re-validated on the way in. If I try to save a one-character title the API rejects it — the reviewer is not a way around the contract."

## 4. Reject one

Reject the vague cost-numbers item.

> "`Rejected` is terminal. It is not a soft delete — there is no path from rejected back to approved, and no path from rejected to execute."

## 5. Try to jump the gate

Press **Execute** on an item that is still `Extracted`.

> "The server refuses it: `409 execution_not_allowed`. The button being visible is not what protects us — I could do the same thing with `curl` and get the same refusal. The rule lives in the execution service."

## 6. Approve

Approve an item that has a deadline. Its label becomes `Approved — ready to execute`, and its edit button disappears.

> "Approval freezes the content. What gets executed is exactly what was approved — you cannot change it underneath the approval."

## 7. Execute

Press **Execute** on the approved item.

> "Now, and only now, do we talk to Google. One Calendar event and one Gmail draft."

The card shows `Executed`, the Calendar event link, and:

> **Draft created — not sent.**

> "That is not a UI nicety. The app requests `gmail.compose`, not `gmail.send`, so it cannot send mail. There is no send method in the Gmail client, and a test scans the package to prove it."

Open Gmail in another tab and show the draft. Show the calendar event on the deadline.

## 8. Press Execute again

> "`409 already_executed` — and nothing new in Google."

Explain the idempotency layers: the status gate, the locally recorded event/draft id, the search Google does before creating anything, and the compare-and-set write that makes a concurrent double-click safe.

## 9. Optional: show a failure

Stop the app's network access, or temporarily misconfigure the Calendar id, approve a fresh item and execute it. The item becomes `failed`, the draft that succeeded is preserved, and the error is recorded on the item.

Restore the configuration and press **Execute** again: the item reaches `executed`, and no duplicate draft appears, because the retry reused the one that already existed.

## Where to look in the code

| Claim | File |
| --- | --- |
| The approval gate | `app/services/execution_service.py` (`_ensure_executable`) |
| The state machine as data | `app/models/action_item.py` (`ALLOWED_TRANSITIONS`) |
| Forced structured output | `app/agents/prompts.py`, `app/agents/extraction_agent.py` |
| Drafts-only guarantee | `app/integrations/google_gmail.py` |
| Idempotency | `app/services/calendar_service.py`, `app/services/gmail_service.py`, `app/db/repository.py` (`save_if_status`) |
| Next steps and design rationale | `docs/architecture.md`, and the README's *Design decisions* table |