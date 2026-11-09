# Computer-use automation for legacy bank back-office software

> **In one line:** an LLM learns a legacy back-office workflow once; from then on, a deterministic engine replays it with no model in the loop — with guardrails, full evidence logging, and human handoff when it gets stuck.
>
> **Stack:** Python · Playwright · Claude API (discovery only) | **~90 tests** | Design tradeoffs: [REPORT.md](REPORT.md)

The idea: an LLM figures out how to complete a goal by driving a real (in
this case mocked) legacy back-office web app, one time. That successful run
gets frozen into a typed, versioned **capability artifact**. After that, a
**deterministic replay engine** runs the artifact with no model in the loop
at all, and it doesn't just succeed or crash — it classifies what actually
happened into business outcomes, recoverable conditions, or hard failures.
When it genuinely can't continue on its own, it hands the *same* live browser
session over to a human and picks back up once they're done.

Why it's built this way, and what got cut: **[REPORT.md](REPORT.md)**.

## Setup

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
```

Copy `.env.example` to `.env` and add your key (needed only for the discovery
agent — replay, the guardrails, evidence logging, escalation, and the
catalog all run with **no key and no live LLM**):

```
ANTHROPIC_API_KEY=sk-ant-...
CUA_MODEL=claude-opus-5      # optional; switch to claude-sonnet-5 to cut cost
```

Everything below assumes commands are run from the repo root. On Windows,
`.venv\Scripts\python`; on macOS/Linux, `.venv/bin/python`.

## Demo: run the agent on a goal, then replay the resulting artifact

This is the core loop, start to finish. Nothing else needs to already be
running — each command starts and stops its own copy of the mock app.

```
# 1. A real LLM discovery run against the live app (needs ANTHROPIC_API_KEY)
.venv\Scripts\python scripts\run_discovery.py open_subaccount
#    writes evidence\discovery\<run_id>\{transcript.json, steps.jsonl, run.log,
#    messages.json, step_NN.png, video\}

# 2. Turn that transcript (plus two runs that hit business outcomes) into the
#    saved artifact. Reuses cached transcripts if you already have some from
#    step 1 / evidence\discovery; pass --fresh to force new discovery runs.
.venv\Scripts\python scripts\build_artifact.py open_subaccount
#    writes cua\capabilities\open_subaccount.v1.json

# 3. Deterministic replay of that artifact -- no LLM, no API key needed
.venv\Scripts\python scripts\replay.py open_subaccount member_id=101445 account_type=Savings initial_deposit=250.00 member_authorized=yes --approve
#    writes evidence\replay\<run_id>\{result.json, steps.jsonl}
```

`cua/capabilities/*.v1.json` already has both artifacts built, so step 3
works right away without needing steps 1–2 first — that's effectively the
"run without live services" path, since it makes no LLM call and only needs
the mock app, which the script starts by itself anyway.

### More replay scenarios (still no LLM)

```
# a business outcome, not a crash -- HTTP-clean typed result either way
.venv\Scripts\python scripts\replay.py open_subaccount member_id=999999 account_type=Savings initial_deposit=250.00 member_authorized=yes --approve
.venv\Scripts\python scripts\replay.py open_subaccount member_id=100987 account_type=Savings initial_deposit=250.00 member_authorized=yes --approve   # Dormant member -> escalate=true

# the read-only capability
.venv\Scripts\python scripts\replay.py read_member_balance member_id=100234 account_type=Savings

# a recoverable condition, handled automatically
.venv\Scripts\python scripts\replay.py read_member_balance member_id=100234 account_type=Savings --interstitial
```

### Human escalation & handoff

This pauses the run, writes out an intervention ticket, and hands the exact
same live browser session to a mock operator over CDP. Run the first command
below; once it pauses it prints a second command with the ticket id filled
in — run that one too (a second terminal makes it easier to watch both at
once, but isn't required):

```
.venv\Scripts\python scripts\replay.py open_subaccount member_id=101445 account_type=Savings initial_deposit=250.00 member_authorized=yes --escalate
# ... prints: python scripts\operator.py <ticket_id>
.venv\Scripts\python scripts\operator.py <ticket_id>

# an obstacle no artifact's taxonomy has ever seen -- shows the "stuck"
# path is real and not just the interstitial replay already handles alone
.venv\Scripts\python scripts\replay.py read_member_balance member_id=100234 account_type=Savings --escalate --unexpected-notice
.venv\Scripts\python scripts\operator.py <ticket_id> --dismiss-notice
```

### Cross-tenant reuse

The same `read_member_balance` artifact, recorded against the base app,
replayed against a second tenant build three ways: unmodified (blocked before
it touches the tenant at all), re-authorized but without the locator fix
(fails cleanly on the one step that actually differs), then with the
checked-in overlay applied (replays end to end). See REPORT.md §
Heterogeneity.

```
.venv\Scripts\python scripts\demo_tenant.py
# starts a second copy of the mock app on port 5002, configured as the
# Harborview tenant; writes each stage's evidence under
# evidence\tenant_harborview\
```

### Capability catalog (stretch goal)

```
.venv\Scripts\python scripts\demo_catalog.py
# starts uvicorn cua.catalog:app as a real subprocess, calls it over real
# HTTP (list capabilities, fetch a tool schema, invoke one), writes the
# exchange to evidence\catalog\http_exchange.json

# or run it yourself and explore:
.venv\Scripts\python -m uvicorn cua.catalog:app --port 8000
# then: GET /capabilities, GET /capabilities/{id}/tool_schema,
#       POST /capabilities/{id}/invoke  {"inputs": {...}, "approve_risky": false}
```

## Tests

```
.venv\Scripts\python -m pytest -q
```

90 tests. Apart from the schema-only ones, they run against the real mock
app and a real Chromium instance — nothing here is mocked out. The
escalation and catalog suites go further and spawn `scripts/operator.py` /
`uvicorn` as genuinely separate OS processes for a couple of true end-to-end
tests. You don't need `ANTHROPIC_API_KEY` to run any of this; discovery is
tested against frozen transcript fixtures rather than a live model.

One side effect: because these tests exercise real code paths instead of
mocks, running the suite does leave a few extra evidence files behind
(replay results, escalation tickets) under `evidence/`. Harmless, and fine to
delete if you'd rather keep the tree clean.

## Project layout

```
bankapp/            mock legacy back-office target app (Flask)
cua/
  schema.py           the capability artifact + replay result models
  perception.py, agent_tools.py, discovery.py     discovery agent loop (LLM)
  synthesize.py        transcript -> parameterised artifact
  locate.py, replay.py, textnorm.py     deterministic replay (no LLM)
  guardrails.py, redaction.py, config.py     allow/deny-list, redaction
  evidence.py          structured per-step logging (both loops)
  escalation.py         human handoff ticket protocol
  overlay.py             per-tenant artifact overrides
  catalog.py              agent-facing HTTP interface (stretch goal)
cua/capabilities/     the two saved artifacts
cua/capabilities/overlays/     the one tenant overlay (Harborview)
scripts/             CLIs for every demo above
tests/               90 tests
evidence/            discovery/replay/escalation/catalog/tenant run evidence
REPORT.md            design write-up (7 required headings)
```

## Notes

- `bankapp/` is styled to look like the internal tool this project is
  actually meant for: table-based layout, no test IDs, no client-side
  framework. `POST /admin/reset` restores seed data for tests and demos, and
  it's hard-denylisted so neither the agent nor replay can ever reach it (see
  REPORT.md § Safety).
- `.env` is git-ignored and no secrets are committed anywhere.
  `cua/redaction.py` masks declared-sensitive values and common PII patterns
  before any of it touches disk.
