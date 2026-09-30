"""Run the discovery runs needed for a capability and synthesise its artifact.

    python scripts/build_artifact.py open_subaccount
    python scripts/build_artifact.py read_member_balance
    python scripts/build_artifact.py all

Each capability needs one successful run (the step sequence) plus a couple of
runs that hit terminal business outcomes (so the outcome taxonomy is real).
Writes cua/capabilities/<id>.v1.json.
"""

from __future__ import annotations

import argparse
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from cua._appserver import running_bankapp
from cua.config import load_guardrail_config
from cua.discovery import DiscoveryConfig, TargetSpec, run_discovery
from cua.schema import (
    BusinessOutcomeRule, Condition, ConditionType, Confidence, HardFailureRule,
    Locator, LocatorKind, LocatorStrategy, ParamType, RecoverableRule,
    RecoveryStrategy,
)

ACCOUNT_TYPES = ["Savings", "Checking", "Money Market", "Certificate of Deposit"]
from cua.synthesize import business_rule_from_run, transcript_to_artifact

BASE_URL = "http://127.0.0.1:5001"
APP = dict(app_id="meridian-backoffice", app_version="3.11.2")
_GUARDRAILS = load_guardrail_config()
ALLOW_DISCOVERY = _GUARDRAILS["allowed_url_patterns"]
ALLOW_REPLAY = _GUARDRAILS["allowed_url_patterns"]
TARGET = TargetSpec(base_url=BASE_URL, entry_path="/members/search", **APP)


def baseline_rules() -> list:
    """Authored (not discovered) conventions for this vendor product."""
    return [
        RecoverableRule(
            id="maintenance_interstitial",
            description="Authored: a 'System Notice' maintenance page precedes "
            "the requested page and must be acknowledged.",
            detect=[Condition(type=ConditionType.TEXT_PRESENT,
                              value="Scheduled maintenance window is in effect")],
            strategy=RecoveryStrategy.CLICK_AND_CONTINUE,
            recovery_target=Locator(
                intent="the Continue link on the maintenance notice",
                strategies=[
                    LocatorStrategy(kind=LocatorKind.ROLE, value="link",
                                    options={"name": "Continue"},
                                    confidence=Confidence.HIGH,
                                    rationale="Single named action on the page."),
                    LocatorStrategy(kind=LocatorKind.TEXT, value="Continue",
                                    confidence=Confidence.MEDIUM,
                                    rationale="Visible-text fallback."),
                ],
            ),
            max_attempts=2, backoff_ms=0,
        ),
        HardFailureRule(
            id="session_timeout",
            description="Authored: the session expired mid-flow; the run cannot "
            "be trusted to continue.",
            detect=[Condition(type=ConditionType.TEXT_PRESENT,
                              value="Your session has expired")],
            failure_code="SESSION_TIMEOUT",
        ),
    ]


import glob
from cua.transcript import DiscoveryTranscript

_REUSE = True  # set by --fresh


def _find_cached(goal, params):
    # the internal build cache (unredacted), not the published evidence copy
    for p in sorted(glob.glob(".cache/discovery/*.json"), reverse=True):
        t = DiscoveryTranscript.model_validate_json(open(p, encoding="utf-8").read())
        if t.goal == goal and t.goal_params == params and t.final_status in {
                "goal_reached", "business_outcome"}:
            return t
    return None


def _run(goal, params, outputs, cfg, sensitive_outputs=None):
    if _REUSE:
        cached = _find_cached(goal, params)
        if cached:
            print(f"  reuse transcript {cached.run_id} ({cached.final_status})")
            return cached
    return run_discovery(goal=goal, goal_params=params, target=TARGET,
                         sensitive_outputs=sensitive_outputs,
                         requested_outputs=outputs, config=cfg)


def build_open_subaccount(cfg) -> None:
    goal = ("Open a new sub-account for the given member with the given account "
            "type and initial deposit, and reach the confirmation screen. The "
            "member has authorized this action (member_authorized = yes). "
            "Report the new sub-account number.")
    outputs = ["sub_account_number"]

    happy = _run(goal, dict(member_id="101445", account_type="Savings",
                            initial_deposit="250.00", member_authorized="yes"),
                 outputs, cfg)
    notfound = _run(goal, dict(member_id="999999", account_type="Savings",
                               initial_deposit="250.00", member_authorized="yes"),
                    outputs, cfg)
    denied = _run(goal, dict(member_id="100987", account_type="Savings",
                             initial_deposit="250.00", member_authorized="yes"),
                  outputs, cfg)

    rules = [
        business_rule_from_run(notfound, rule_id="member_not_found",
                               code="MEMBER_NOT_FOUND",
                               message="No member matches {member_id}.",
                               after_steps=["submit_search"]),
        business_rule_from_run(denied, rule_id="member_not_eligible",
                               code="MEMBER_NOT_ELIGIBLE", escalate=True,
                               message="Member {member_id} is not eligible; a "
                               "supervisor override is required.",
                               after_steps=["click_open", "open_new_form"]),
        *baseline_rules(),
    ]

    art = transcript_to_artifact(
        happy,
        capability_id="open_subaccount",
        name="Open a member sub-account",
        description="Opens a new sub-account for an active member and returns "
        "the new sub-account number from the confirmation screen. Needs the "
        "member number, account type, an initial deposit at or above the "
        "$25.00 minimum, and confirmation that the member authorized the "
        "action. The submit step is irreversible.",
        output_specs={"sub_account_number": {"type": None}},
        extra_rules=rules,
        allowed_url_patterns=ALLOW_REPLAY,
        overrides={
            "step_ids": {"click_open_sub": "open_new_form",
                         "submit_open_sub": "submit_new_subaccount"},
            "risky_step_ids": ["submit_new_subaccount"],
            "params": {"initial_deposit": {"constraints": {"min_amount": "25.00"}}},
        },
    )
    _save(art, [happy, notfound, denied])


def build_read_member_balance(cfg) -> None:
    goal = ("Look up the given member and read the current balance of their "
            "sub-account of the given type. Report the balance, the sub-account "
            "number and its status.")
    outputs = ["balance", "sub_account_number", "account_status"]

    sens = ["balance"]
    happy = _run(goal, dict(member_id="100234", account_type="Savings"), outputs, cfg, sens)
    notfound = _run(goal, dict(member_id="999999", account_type="Savings"), outputs, cfg, sens)
    absent = _run(goal, dict(member_id="100987", account_type="Savings"), outputs, cfg, sens)

    rules = [
        business_rule_from_run(notfound, rule_id="member_not_found",
                               code="MEMBER_NOT_FOUND",
                               message="No member matches {member_id}.",
                               after_steps=["submit_search"]),
        # the app has no distinct panel for "member found, but no sub-account
        # of this type" -- unlike member-not-found / not-eligible it isn't an
        # error state, it's an absence, so detect it structurally (the type
        # string missing from the page) rather than by matching a banner.
        BusinessOutcomeRule(
            id="subaccount_type_absent",
            description="The member was found but has no sub-account of the "
            "requested type. Confirmed by a discovery run against member "
            f"{absent.goal_params['member_id']}: {absent.final_summary[:100]}",
            detect=[Condition(type=ConditionType.TEXT_PRESENT, value="Sub-Accounts"),
                    Condition(type=ConditionType.TEXT_ABSENT, value="{account_type}")],
            after_steps=["click_open"],
            outcome_code="SUBACCOUNT_TYPE_NOT_FOUND",
            message_template="Member {member_id} has no {account_type} sub-account.",
            terminal=True, escalate=False,
        ),
        *baseline_rules(),
    ]

    art = transcript_to_artifact(
        happy,
        capability_id="read_member_balance",
        name="Read a member's sub-account balance",
        description="Looks up a member, opens their detail page and returns the "
        "balance, sub-account number and status for a given sub-account type. "
        "Read-only. The balance is regulated data: returned to the caller, "
        "never written to logs in the clear.",
        output_specs={
            "balance": {"type": None, "sensitive": True},
            "account_status": {"required": False},
        },
        extra_rules=rules,
        allowed_url_patterns=ALLOW_REPLAY,
        # this flow never opens the type dropdown, so the synthesiser can't see
        # that account_type is a fixed set -- declare it here.
        overrides={"params": {"account_type": {
            "type": ParamType.ENUM, "enum_values": ACCOUNT_TYPES}}},
    )
    _save(art, [happy, notfound, absent])


def _submit_steps(t):
    """Heuristic: the click step that leaves a /sub-accounts/new page."""
    real = [s for s in t.steps if s.tool_name == "click"]
    return [s for s in real if s.captured_element
            and (s.captured_element.name or "").lower().startswith("open sub-account")
            and s.captured_element.role == "button"]


def _save(art, transcripts) -> None:
    from pathlib import Path
    out = Path("cua/capabilities") / f"{art.capability_id}.v{art.version}.json"
    art.save(out)
    print(f"\nwrote {out}")
    print(f"  {len(art.steps)} steps | {len(art.input_params)} params | "
          f"{len(art.outputs)} outputs | {len(art.outcome_taxonomy)} outcome rules")
    print("  discovery runs used:")
    for t in transcripts:
        print(f"    {t.run_id}  {t.final_status:16} {t.final_summary[:70]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("which", choices=["open_subaccount", "read_member_balance", "all"])
    ap.add_argument("--max-steps", type=int, default=18)
    ap.add_argument("--fresh", action="store_true",
                    help="run new discovery instead of reusing saved transcripts")
    args = ap.parse_args(argv)

    global _REUSE
    _REUSE = not args.fresh

    cfg = DiscoveryConfig(max_steps=args.max_steps, headless=True,
                          allowed_url_patterns=ALLOW_DISCOVERY)
    builders = {"open_subaccount": build_open_subaccount,
                "read_member_balance": build_read_member_balance}
    todo = list(builders) if args.which == "all" else [args.which]

    with running_bankapp(base_url=BASE_URL):
        for name in todo:
            print(f"\n=== building {name} ===")
            builders[name](cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
