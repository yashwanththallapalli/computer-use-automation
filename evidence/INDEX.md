# Evidence index

Everything here is a real run — no fabricated logs. This file points at one
good example of each required kind; the surrounding directories hold more
(both capabilities, every outcome type, several escalation scenarios) because
re-running the demos during development was cheap and each run is genuine
evidence, not filler.

## The saved artifacts

- [`cua/capabilities/open_subaccount.v1.json`](../cua/capabilities/open_subaccount.v1.json) —
  action capability (search → detail → form → confirm), one irreversible step
- [`cua/capabilities/read_member_balance.v1.json`](../cua/capabilities/read_member_balance.v1.json) —
  read-only capability, one `sensitive` output

Both have `metadata.source: "discovery"` and `metadata.discovery_run_id`
pointing at the real run below that produced them.

## One real discovery run (LLM, required)

- [`discovery/20260911T124313Z/`](discovery/20260911T124313Z/) —
  `open_subaccount`, goal reached. `transcript.json` (full structured record:
  every observation, the model's reasoning, each tool call and its result),
  `steps.jsonl` (crash-survivable per-step log), `run.log`,
  `messages.json` (raw Anthropic message log, redacted, decoupled from the
  artifact), `step_00.png … step_09.png`, `video/`.
- [`discovery/20260911T124409Z/`](discovery/20260911T124409Z/) — same, for
  `read_member_balance`.
- `discovery/20260911T124341Z/`, `.../124351Z/`, `.../124429Z/`,
  `.../124440Z/` — the extra discovery runs (member-not-found, dormant
  member) whose terminal banners became the artifacts'
  `outcome_taxonomy` business-outcome rules (see REPORT.md § Artifact schema).

## A clean replay (required)

- [`replay/20260914T190644Z/result.json`](replay/20260914T190644Z/result.json) —
  `open_subaccount`, `status: "success"`, no LLM involved.
- [`replay/20260914T190730Z/result.json`](replay/20260914T190730Z/result.json) —
  `read_member_balance`, `status: "success"`; note `balance` is present in the
  returned result but is masked in this saved copy (it's a declared-sensitive
  output — see REPORT.md § Safety).

## A replay that hits an error / business outcome (required)

- [`replay/20260914T190656Z/result.json`](replay/20260914T190656Z/result.json) —
  `MEMBER_NOT_FOUND`, a typed business outcome, not a crash.
- [`replay/20260914T190702Z/result.json`](replay/20260914T190702Z/result.json) —
  `MEMBER_NOT_ELIGIBLE` (Dormant member), `escalated: true`.
- [`replay/20260914T190707Z/result.json`](replay/20260914T190707Z/result.json) —
  a genuine `FailureResult`: `APPROVAL_REQUIRED`, with the failed step,
  expected/observed, and a failure screenshot + DOM snapshot alongside it.
- [`replay/20260914T190746Z/result.json`](replay/20260914T190746Z/result.json) —
  `SUBACCOUNT_TYPE_NOT_FOUND`, detected structurally rather than by matching a
  banner (the app has no dedicated panel for this state — see REPORT.md).

## Human escalation & handoff

- [`escalations/c8508524ac13.json`](escalations/c8508524ac13.json) —
  `approval_required`, resolved `approved` by a genuinely separate
  `scripts/operator.py` process connected over CDP to the paused session →
  [`replay/20260914T192522Z/result.json`](replay/20260914T192522Z/result.json)
  (`escalations: ["c8508524ac13"]`, `status: "success"`).
- [`escalations/8647b2d0769e.json`](escalations/8647b2d0769e.json) — `stuck`
  (`LOCATOR_UNRESOLVED` on a deliberately unmodeled obstacle,
  `BANKAPP_UNEXPECTED_NOTICE=1`), resolved `recovered` →
  [`replay/20260914T192452Z/result.json`](replay/20260914T192452Z/result.json)
  (retried the same step, then succeeded).
- [`escalations/17bc046a9dda.json`](escalations/17bc046a9dda.json) —
  `review` (a `MEMBER_NOT_ELIGIBLE` business outcome with `escalate: true`),
  left `status: "open"` — correct: this category doesn't block the run, it's
  a side-channel ticket for a human to close later →
  [`replay/20260914T192505Z/result.json`](replay/20260914T192505Z/result.json).

## Cross-tenant reuse

- [`../cua/capabilities/overlays/read_member_balance.harborview.v1.json`](../cua/capabilities/overlays/read_member_balance.harborview.v1.json) —
  the checked-in `TenantOverlay` for a second, differently-built tenant.
- `tenant_harborview/replay/` — three runs of the same base artifact against
  that tenant: unmodified (`BLOCKED_URL`, guardrails catch the unauthorized
  host before anything else runs), re-authorized but without the locator fix
  (`LOCATOR_UNRESOLVED` at `click_open`, the one step that actually differs),
  and with the overlay applied (`success`, same balance as the base-app run
  above). Produced by `scripts/demo_tenant.py`; see REPORT.md § Heterogeneity.

## Capability catalog (stretch goal)

- [`catalog/http_exchange.json`](catalog/http_exchange.json) — a real
  `uvicorn` process called over real HTTP: health check, capability list, a
  generated Anthropic-style tool schema, and one capability invoked for real.

## Reproducing any of this

See `README.md` § Demo — every path above was produced by one of the exact
commands listed there.
