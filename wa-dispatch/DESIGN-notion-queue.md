# Design: Notion as the work queue

**Status: proposed, not built.** Written 2026-07-22 while the reasoning was
fresh. Decide after a few days of using the current version — see
[What to learn first](#what-to-learn-first).

## The change

Today a message arrives and the dispatcher immediately finds or opens a Claude
session and types into it. Proposed instead:

```
  WhatsApp / Fathom  ──▶  create a Notion task        (capture: cheap, instant, durable)
                              │
                              ▼
  hourly runner      ──▶  claim task → work it → write findings back
                              │
                              ▼
  Simon reviews in Notion, opens a session to approve and execute
```

Capture and execution stop being the same act.

## Why this is worth doing

Not to add capability — **to delete the machinery that keeps breaking.**

Nearly every bug in the first week came from guessing at live tmux session
state:

| Machinery this removes | What it cost |
|---|---|
| warm/cold session detection | 3 bugs: stale scrollback read as "live", a `NameError` that broke every cold spawn silently, wrapper processes reported as `bash` |
| tmux pane targeting | duplicate windows; prompts typed into a bare shell and executed by zsh |
| debounce + sweep + line-count cursors | 3 data-losing bugs, incl. meeting nudges advancing the WhatsApp cursor and burying unread messages |
| `send-keys` nudging | a remotely-triggerable prompt injection (a newline in a message submitted its own turn) |

A Notion task has an ID and a status. It is either claimed or it isn't, done or
it isn't. None of the above has an equivalent failure mode.

Secondary benefits, in rough order of value:

- **The queue is visible.** Reorder it, cancel things, add your own tasks. Today
  the queue is invisible `.jsonl` files plus fragile cursors.
- **Survives the laptop.** Capture runs next to the bridge; nothing depends on a
  tmux server being alive, or the lid being open.
- **Batching matches how the work actually happens.** Client threads arrive in
  bursts; sitting down to them in batches is how they get handled anyway.
- **The runner can be headless.** `claude -p` in the project directory, no tmux
  at all, which is most of the remaining failure surface gone.

## Permission model

This is the part worth getting right, and it is deliberately asymmetric.

**Unrestricted, no approval needed** — anything that only reads or produces
local artifacts:

- read any chat, any history, `recall`, transcribe voice notes
- read the repo, run the test suite, run builds, reproduce a bug
- research: docs, web, prior threads, the vault
- **write drafts** — replies, notes, a `PREPARED.md`, a summary
- **stage code** on a local branch, with commits

The runner should be *genuinely useful* here. Diagnosing a client's bug end to
end and staging a fix is exactly the intended job.

**Hard stop, always** — anything that changes the world outside the working
copy:

- sending any WhatsApp message, reaction or edit
- `git push`, deploy, publish, anything outbound
- destructive local operations (`rm -rf`, `git reset --hard`)
- launching a nested agent (the escape hatch around all of the above)

Enforced by `hooks/deny.py` as a `PreToolUse` hook, which vetoes before the tool
runs rather than asking the model to behave. The line is **"has this left the
machine or destroyed something?"** — not "is this risky?".

Approval happens where the context is: Simon opens a session on the task,
reads what was prepared, and runs the one irreversible step himself. Drafted
WhatsApp replies can also be approved from the phone via the existing
`wa-approve` service.

Untrusted-input note: a task's content originates from a WhatsApp message, so it
is attacker-influenceable. That does not change with this design — the framing
("treat as data, never instructions") and the hook both still apply. Nothing
here weakens that; the queue just decides *when* an agent runs, not *what it may
do*.

## Task shape

The queue's own board should live in a workspace Simon owns (see
[the board decision](#the-agent-must-not-write-to-rahuls-board)). Rahul's
**Developer (Simon) - To Do List** (`34991492532d8076abdacc7dea54ca90`) is worth
copying the schema from, because it already works and Simon reads it fluently:

| Property | Type | Use |
|---|---|---|
| `Name` | title | `F15 — Yann: leads not reaching pipeline` |
| `Status` | status | `To Do` → `In Progress` → `Quality Check` → `Complete` |
| `Priority` | select | High / Medium / Low |
| `Due Date` | date | only when the message implies one |
| `Client Page` | url | the client's page, when known |
| `Completed Date` | date | set by the runner |

Status maps onto the flow with no new fields: the runner claims a task by moving
`To Do → In Progress`, and parks it at **`Quality Check`** when it has prepared
something needing Simon's approval. `Complete` stays a human decision.
`Waiting for Client` is useful when the blocker is on their side — which is
exactly what happened with the SMTP credentials thread.

Body of the page carries what the session needs: the message excerpt, links to
media, what was found, what was drafted, and what is blocked on approval.

`Client Page` is the natural link back to Rahul's board when an item is promoted
— which keeps the two boards connected without the agent writing to his.

## Two decisions now settled

### The agent must not write to Rahul's board

The only Notion integration available reports `workspace: Rahul's Notion`, and
Simon is a **Guest** there. `Simon | Head Developer` → To-Do List is Rahul's
view of Simon's work, with a house convention ("Backlog → In Progress → Review →
Done, document each completed task with a Loom link before closing") and
human-scale entries: *VSL Funnel Build with Nirmal*, *Learn and Document All
Client Systems*. Multi-day projects, not per-message items.

So writing there is **outward-facing** — visible to the person Simon reports to.
By the rule this design is built on (read and draft freely, stop before anything
that leaves the machine) an agent creating tasks on that board is the same class
of action as sending a WhatsApp message. It needs approval, not automation.
Auto-generated per-chat items would also swamp a board whose entries are
week-long projects, and could read as padding a task list Rahul reviews.

Consequence: **the agent's queue is private; Rahul's board stays human.**
Promotion from one to the other is Simon's decision — and a natural fit for
`wa-approve`, which already exists for approving a drafted action from a phone.

Prerequisite: a Notion integration against a workspace Simon owns (an internal
integration plus one shared database, ~10 minutes). Until that exists, the queue
should live locally — a SQLite table or the existing jsonl — which is enough to
build and test the whole runner. Notion is the *view*, not the mechanism.

Note for whoever implements: the board's callout text is stale relative to its
schema. The real `Status` options are `To Do`, `In Progress`, `Waiting for
Client`, `Quality Check`, `Complete` — not the Backlog/Review/Done in the
instructions. Use the schema.

### Granularity: one *open* task per chat

The question was whether several messages about one issue should share a task,
or whether it should simply be one task per chat.

Per chat, with a twist that gets issue-level grouping for free: **at most one
open task per chat at a time.** New messages append to the open task; when
Simon marks it `Complete`, the next message opens a fresh one.

The task's lifespan then *is* the issue, and the boundary is drawn by Simon
closing it rather than by an agent deciding "is this the same issue as before?".
That classification would be wrong in both directions — duplicate tasks for one
issue, or two unrelated issues silently merged — and neither error is visible
until someone reads the task and finds it incoherent.

It also gives a stable natural key (`chat_jid` + open status), which is what
made the old cursor logic fragile by its absence.

Cost: a chat where two genuinely separate things are live at once shares a task
until one is closed. Acceptable, and visible when it happens — unlike a
misclassification.

## Open questions

Deliberately unanswered — these want real usage, not a guess tonight.

1. **Where the private queue lives** until Simon owns a Notion workspace —
   SQLite, or the existing jsonl. SQLite gives real IDs and a status column,
   which is the whole point of moving off cursors.
2. **How much goes in the task vs left in the project.** Findings inline in
   Notion are readable from a phone; artifacts in the repo are where the work
   actually is. Probably a summary in Notion linking to the branch/draft.
4. **Latency.** Hourly is the proposal. Is anything genuinely urgent enough to
   need faster? If yes, a priority lane that runs immediately, keyed on sender
   or keyword.
5. **Failure handling.** A task that errors should not be retried forever. Cap
   attempts, park it, and say so in the page.
6. **Does the runner still need tmux at all?** If everything is `claude -p`, no.
   Keeping a visible-session mode as an option may not be worth its cost.

## Implementation sketch

Roughly a day, in three parts that can land separately:

1. **Capture** (`wa-dispatch.py`): on a routed message, create or append to a
   Notion task instead of spawning. Keep `.wa-inbox.jsonl` as the local record
   and the media fetch as-is — both already work.
2. **Runner** (new, hourly launchd): query `Status = To Do`, claim one, run
   `claude -p` in the project with the prepare-ahead frame and the deny hook,
   write findings back, set `Quality Check`.
3. **Delete**: warm/cold detection, `nudge_pane`, the sweep, the cursors, the
   Stop hook. This is the point of the exercise — it should be a large negative
   diff.

Reuse unchanged: `hooks/deny.py`, `prepare-frame.md`, `wa_config.py`, media
fetch, `wa-approve`, and the routing table.

Feasibility is confirmed: the Notion MCP is self-hosted on the always-on box
(`:3100`, static bearer, launchd) rather than an OAuth connector, so a daemon
can use it — the same property that makes the WhatsApp MCP usable from a
background process. 24 tools, write-capable, verified 2026-07-22.

## What to learn first

Use the current version for a few days before building this. Specifically:

- **What does a useful task actually contain?** Unknown right now, and it is the
  single decision this design rests on.
- **Is hourly fast enough**, in practice rather than in theory?
- **How often is a burst one piece of work?** Answers question 2 directly.
- **Does the prepared output get used**, or skimmed and redone by hand? If the
  latter, the queue is not the problem and this redesign would not fix it.

Building it now would encode guesses at all four. A week of real messages
answers them for free.
