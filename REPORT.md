# REPORT

## Architecture

Three things happen in sequence, and they only share one thing: an artifact
format. First, an LLM drives a real browser and figures out how to complete a
goal. Once that succeeds, the run gets frozen into a typed **capability
artifact**. From then on, a **replay engine** runs that artifact with no model
involved at all. A human escalation path sits underneath both the discovery
loop and replay for when neither can proceed alone, and a small FastAPI
catalog (the stretch goal) lets an outside agent call a saved capability by
name.

```
bankapp/        mock legacy back-office target (Flask, table-based, no test IDs)
cua/schema.py   the artifact + result contract every other module shares
cua/perception.py, agent_tools.py, discovery.py   the discovery loop (LLM)
cua/synthesize.py    transcript -> parameterised artifact
cua/locate.py, replay.py, textnorm.py   deterministic execution (no LLM)
cua/guardrails.py, redaction.py, config.py   allow/deny-listing, redaction
cua/evidence.py      crash-survivable structured logging, shared by both loops
cua/escalation.py, scripts/operator.py   human handoff
cua/catalog.py        agent-facing HTTP interface (stretch)
```

A handful of decisions determined most of what came after them.

The artifact is a flat, ordered list of steps, not a state graph — branching
lives in one place, the `outcome_taxonomy`, as data rather than scattered
across step topology or exception handling. Locators are accessibility-tree-
first with fallback chains, not screenshots or pixel coordinates: even a
legacy app with zero test IDs usually still exposes real ARIA roles
underneath, and that survives a redesign in a way (x, y) coordinates never
will. Everything runs as one synchronous Python process per invocation, no
queue or service mesh — the brief is explicit that scaling infrastructure
isn't what's being evaluated, and it would be overkill here regardless. The
one place two processes genuinely have to talk (a paused replay run and the
operator resolving it) is a polled JSON file on disk, not a socket.

Stack: Python 3.12, Playwright, Pydantic v2, Anthropic's `claude-opus-5`.
Pydantic does real cross-field validation on construction, not just type
checking, which matters once an artifact has cross-references between steps,
outputs, and outcome rules. Playwright's accessibility-first locators and its
CDP support are what make both the locator strategy and the human handoff
work without extra tooling bolted on.

## Artifact schema

`CapabilityArtifact` (`cua/schema.py`) is what discovery produces, what
replay consumes, what a human reviews, and — through the catalog — what a
calling agent sees. It carries an id, version, and approval state; a `target`
(which app, tenant, surface); typed `input_params` and `outputs`; the ordered
`steps`; a `success_checkpoint`; the `outcome_taxonomy`; a guardrail `policy`;
and `metadata` that points back at the discovery run and its raw transcript
without embedding it.

Three parts of this actually carry weight.

**Locators are ordered fallback chains, not single selectors.** Each
`LocatorStrategy` names a `kind` — role, a label to search near, a table cell
by column, CSS, XPath — along with a `confidence` and a written `rationale`.
At replay time the first strategy that resolves to exactly one element wins,
and the engine records *which one* (`"table_cell#0"`, `"css#2"`). If it's
always `#0`, the artifact is holding up fine. A rising index over time is a
drift signal worth looking at, even though the capability still technically
works.

**`outcome_taxonomy` is where the branching actually lives.** Each rule is
one of `business_outcome`, `recoverable`, or `hard_failure`, paired with the
conditions that detect it. "No such member" comes back as a typed, terminal
result the caller is expected to handle — never an exception. A maintenance
interstitial gets dismissed and replay just continues. Anything unrecognized
stops with full detail attached. The brief calls conflating these three the
single most common mistake in this space, so I wanted it enforced at the
schema level rather than left to whoever writes the try/except blocks later.

**Anything variable is a `{param}` placeholder**, filled in at replay time
from the caller's typed inputs. This applies inside step values and also
inside nested locator options (`row_contains: ["{member_id}"]`), so a
row-scoped locator generalizes to every member rather than only the one that
happened to be on screen during discovery.

## Determinism & error handling

`cua/replay.py` never touches the model. Per step: resolve the locator chain,
act once, then loop over the outcome taxonomy. Recoverable rules are checked
*before* business or hard-failure rules, on purpose — a page that happens to
satisfy a "not found" text-absence check is often really an unmodeled
interstitial sitting in the way, and checking the terminal rules first would
misreport something recoverable as final. When a recoverable rule matches,
its recovery runs and then the check re-runs *without re-acting*; the
recovery itself, not another click, is what should get the page back to where
the step expected it to be. Only once every step has passed does the
`success_checkpoint` get verified and the required outputs confirmed present.

The result is a discriminated union: `Success` with typed outputs,
`BusinessOutcome` with a code and message and an `escalated` flag, or
`Failure` with a code, the step it happened on, expected versus observed, a
screenshot, and a full DOM snapshot. None of that needs digging through logs
— it's all on the object itself. Separately, every run writes `steps.jsonl`
as it goes, one line flushed right after each step completes, so a killed
process still leaves a usable trail even though the final result file never
got written.

I built this loop by running it against the live app and fixing whatever
broke, before I wrote a single test for it. That surfaced five real bugs
(all of them are now regression tests in `tests/test_locate.py` and `tests/test_replay.py`): a label search that matched
the page's own layout wrapper before it found the actual field; `{param}`
placeholders that weren't resolving inside nested locator options; an outcome
rule with no distinct banner to match, so it matched the wrong page instead;
a recovery that re-ran an entire step rather than just re-checking it; and a
checkpoint URL pattern too strict to allow a query string a legitimate
recovery had every right to add. All five are regression tests now
(`tests/test_locate.py`, `tests/test_replay.py`).

## Heterogeneity & multi-tenant

The surface story below is design only — the brief scopes implementation to
one surface, and I didn't second-guess that. Multi-tenant reuse I did
implement and prove, not just design, since "reuse across institutions
running the same app" is exactly the case the brief's stretch goal is asking
about.

The seam for other surfaces is `cua.perception` on the observing side and
`cua.locate.Resolver` / `cua.agent_tools` on the acting side, both sitting in
front of the surface-neutral `steps`/`Locator` model — `TargetBinding.surface`
already names where this could go (`web_dom`, `web_a11y`, `desktop_a11y`,
`screenshot_coords`). A legacy web app built on framesets would need a
`frame_scoped` locator kind and a perception pass that walks into frames,
with neither the artifact schema nor the replay loop needing to change. A
desktop app would mean swapping `cua.browser.BrowserSession` for a native
accessibility driver (Windows UIA, macOS AX) behind the same `Resolver`
interface; `Step`, `Condition`, and `outcome_taxonomy` don't assume a browser
anywhere, so they wouldn't need to change either.

For multi-tenant reuse, `cua/overlay.py` has `TenantOverlay` and
`resolve_effective_artifact`. A base artifact (`tenant_id: null`) is the
vendor-generic recording; a tenant that genuinely differs gets a small diff
instead of a whole new recording — override the `base_url`, `app_version`, or
allowlist, prepend extra locator strategies onto specific steps (the base
chain stays as a fallback under them), disable or add a step here and there.

`bankapp` now actually runs as a second tenant (`BANKAPP_TENANT=harborview`):
same routes, same data, different branding, an extra column in search
results, and the one row action relabeled — the kind of difference a config
knob in the vendor's own product would produce, not a different app.
`scripts/demo_tenant.py` (and `tests/test_tenant_overlay.py`, which is the
real regression test) replay the base `read_member_balance` artifact against
it three ways: unmodified, it never gets past guardrails, because its
allowlist is still anchored to Meridian's host; re-authorized but with the
overlay's locator fix withheld, it reaches Harborview's search results and
fails cleanly at the one step that differs (`click_open`,
`LOCATOR_UNRESOLVED`); with the checked-in overlay
(`cua/capabilities/overlays/read_member_balance.harborview.v1.json`) applied,
it replays end to end, and every step but that one still resolves on the base
chain's own primary strategy. Evidence for all three is under
`evidence/tenant_harborview/`.

That middle stage is also why `policy_overrides` is its own field on
`TenantOverlay`, separate from `target_overrides.base_url`: a tenant on a new
host needs its own allowlist entry, stated explicitly, not derived from
`base_url` and trusted blind. Building that stage is what surfaced a real
enforcement gap in replay itself — see Safety.

Drift detection can ride on a signal replay is already producing: since
`StepTrace.locator_used` records which strategy actually resolved on every
run, a fleet-wide job (not built here) watching that across a tenant's
history — "this tenant falls through to strategy #2 on this step most of the
time" — is the trigger to write an overlay, instead of re-recording blind.

## Escalation & handoff

Before writing anything around it, I checked that the actual mechanism
worked: launch a browser with a CDP debug port open, then from a second,
completely separate OS process, call `connect_over_cdp` and read and change
the page. The change showed up back on the first process's own page handle.
Everything downstream is built on that one fact holding.

The protocol is a `HandoffTicket` — one JSON file per ticket, under
`evidence/escalations/`. Replay writes it and polls for `status != "open"`; a
separate operator process reads it, connects to the CDP endpoint it names,
does whatever's needed on the live page, and writes the resolution back.
There's no socket and no server involved, and it survives either side being
restarted.

There are three trigger categories, and they resume differently on purpose —
treating them the same would either double an action that already happened or
sit there waiting for something a human can't actually fix:

| Trigger | Blocks the run? | On resume |
|---|---|---|
| `CONFIRM` step blocked (no approval yet) | Yes | Approval covers the step right after the gate for the rest of the run; automation performs the write itself — the human is authorising, not standing in for the click |
| `LOCATOR_UNRESOLVED`, i.e. "stuck" | Yes | Retries the same step from scratch. Only safe because nothing was acted on yet — the exception fires before any `.click()` or `.fill()` runs |
| A `BusinessOutcomeRule` flagged `escalate: true` (say, a member who isn't eligible) | No | The typed result comes back right away; the ticket is a side channel so a human still sees it and can act outside this run — nothing on the page itself fixes ineligibility |

To show the "stuck" path honestly, the mock app got one more flag,
`BANKAPP_UNEXPECTED_NOTICE=1`, which throws up a page no artifact's taxonomy
has ever encountered — unlike the maintenance interstitial, which replay
already knows how to dismiss on its own. Both blocking paths are proven
end-to-end with `scripts/operator.py` launched as a real `subprocess`, never
a same-process shortcut (`tests/test_escalation.py`).

What this doesn't cover: the browser stays alive only because it's sitting
inside replay's own blocked Python call. That's one long-lived process
holding the session, not two independent ones that could each be restarted on
their own. A production version would need the browser to outlive whatever
process launched it — a detached subprocess, or some kind of session broker —
for replay itself to be restartable while a run is paused.

## Safety

Two guardrail layers, not one. A configurable allowlist
(`config/guardrails.json`, URL patterns plus action types) is what "this may
only touch `/members/*`" means in practice. Underneath that sits a small,
hardcoded denylist — just `/admin(/|$)`, covering the reset endpoint — that no
allowlist entry can reopen. The reasoning: a too-broad allow pattern is a
config mistake someone could plausibly make; a denylist hit means something's
actually wrong and should fail loudly. Every action type is checked in both
loops now, including `extract`, which I found ungated in discovery partway
through and fixed.

Risky actions work through `policy.risky_step_ids`. The synthesiser inserts
an explicit `CONFIRM` step right before each one automatically. Both the
`CONFIRM` step and the risky action after it are blocked without
`approve_risky` set — belt and suspenders. Without escalation turned on,
that's just a clean stop. With it, it becomes a genuine approval gate a human
resolves.

The URL allowlist itself had a real gap until building the second tenant
(below) exercised it: replay constructed a `Guardrail` with the artifact's
`policy.allowed_url_patterns` but never actually called `check_navigation`
during a run, so nothing enforced it — a mispointed `base_url` would just
work. Fixed by checking before every `navigate` and after every `click`, the
same two points discovery already checked. Two existing tests were quietly
depending on the old, permissive behavior and needed a matching allowlist
entry added once enforcement became real.

Redaction is schema-driven: a param or output flagged `sensitive` gets masked
by exact value match, and a regex backstop for email, phone, SSN, and date of
birth runs regardless of what got flagged. The part worth being explicit
about is *when* this happens — once, at write time, over objects that carry
real values the whole way through a run, rather than redacted incrementally
as data moves around. I found two actual leaks this way, by going back and
reading my own already-published evidence instead of just trusting the
mechanism: a sensitive output (a balance) wasn't being masked because only
inputs were redacted at the time (Phase 6), and one of two discovery entry
points had simply never been told which outputs counted as sensitive at all
(Phase 7). Both have regression tests now.

A couple of limits worth naming directly: a failure screenshot or DOM
snapshot can show sensitive data in the clear, traded deliberately against a
failure that leaves no visual trace at all. And redaction only masks the
specific value a capability declares sensitive, not every number that merely
looks like money and happens to be on the page in passing — catching every
dollar-shaped number would make transcripts unreadable when you actually
need to debug something real.

## Cuts

Other surfaces (desktop, iframes/framesets, vision/coordinate locators) are
designed but not built — `SurfaceType` names them and the seam is real (see
Heterogeneity above), but there's one surface here, which is what the brief
asks for. Multi-tenant reuse is the one piece of that section I did go build
a second, real instance for rather than leaving as a diagram — see
Heterogeneity and `evidence/tenant_harborview/`. One tenant overlay is proven;
a fleet of them, and the drift-detection job that would tell me when to write
the next one, are not.

Only one stretch goal — the capability catalog — following the brief's "pick
at most one or two." Skipped: code generation, confidence/approval scoring,
assisted LLM fallback recovery, cross-tenant canonicalisation, and multi-run
stability scoring.

`/invoke` never escalates. It fails fast instead of holding an HTTP request
open waiting on a person. A real version of this would return `202 Accepted`
with a ticket id and let the caller poll, reusing the same protocol Phase 8
already built — no new mechanism needed, just an endpoint I didn't get to.

Auto-retry on "stuck" only covers `LOCATOR_UNRESOLVED`, not `STEP_TIMEOUT`.
A timeout happens *after* the action already ran, so retrying blindly risks
doing it twice. Handling that safely needs some notion of which steps are
actually idempotent, which this build doesn't model.

There's no fleet-wide drift dashboard. The signal for one exists and is
logged (`StepTrace.locator_used`, `ReplayContext.escalations`), but turning
that into an aggregated report across runs or tenants isn't built.

And ticket storage isn't wired up to `evidence_root` the way replay and
discovery evidence now are — a small, known inconsistency, not a correctness
problem.

Given more time, in the order I'd actually do them: the async `/invoke` flow
with tickets, a drift report over `locator_used` that would turn "this tenant
keeps falling through to strategy #2" into something that pages a human
instead of waiting to be noticed, a third tenant to see whether two overlays
already cover most of what a real one needs, and one more surface — nested
iframes or a frameset — deliberately hostile enough to test whether the
heterogeneity claim in the section above actually holds up outside of
design.
