"""Run one real LLM-driven discovery run against the mock bank app.

    python scripts/run_discovery.py open_subaccount
    python scripts/run_discovery.py read_balance --member 100987   # permission-denied path
    python scripts/run_discovery.py open_subaccount --no-headless  # watch it

Needs ANTHROPIC_API_KEY (via .env or env). Starts and stops the mock app itself.
Writes evidence to evidence/discovery/<run_id>/.
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

BASE_URL = "http://127.0.0.1:5001"
APP = dict(app_id="meridian-backoffice", app_version="3.11.2")
ALLOW = load_guardrail_config()["allowed_url_patterns"]

GOALS = {
    "open_subaccount": dict(
        goal="Open a new sub-account for the given member, choosing the given "
             "account type and initial deposit, and reach the confirmation "
             "screen. Report the new sub-account number. The member has "
             "authorized this action (member_authorized = yes), so you may set "
             "the authorization confirmation checkbox on the form.",
        params=dict(member_id="101445", account_type="Savings",
                    initial_deposit="250.00", member_authorized="yes"),
        outputs=["sub_account_number"],
        entry_path="/members/search",
    ),
    "read_balance": dict(
        goal="Look up the given member and read the current balance of their "
             "sub-account of the given type. Report the balance, the "
             "sub-account number and its status.",
        params=dict(member_id="100234", account_type="Savings"),
        outputs=["balance", "sub_account_number", "account_status"],
        entry_path="/members/search",
        sensitive_outputs=["balance"],
    ),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("goal", choices=sorted(GOALS))
    ap.add_argument("--member", help="override member_id")
    ap.add_argument("--account-type", help="override account_type")
    ap.add_argument("--deposit", help="override initial_deposit")
    ap.add_argument("--no-headless", action="store_true")
    ap.add_argument("--max-steps", type=int, default=18)
    ap.add_argument("--interstitial", action="store_true",
                    help="enable the recoverable maintenance interstitial")
    args = ap.parse_args(argv)

    spec = GOALS[args.goal]
    params = dict(spec["params"])
    if args.member:
        params["member_id"] = args.member
    if args.account_type:
        params["account_type"] = args.account_type
    if args.deposit:
        params["initial_deposit"] = args.deposit

    env_extra = {"BANKAPP_INTERSTITIAL": "1"} if args.interstitial else {}
    cfg = DiscoveryConfig(
        max_steps=args.max_steps,
        headless=not args.no_headless,
        allowed_url_patterns=ALLOW,
    )

    with running_bankapp(base_url=BASE_URL, env_extra=env_extra):
        t = run_discovery(
            goal=spec["goal"],
            goal_params=params,
            target=TargetSpec(base_url=BASE_URL, entry_path=spec["entry_path"], **APP),
            requested_outputs=spec["outputs"],
            sensitive_outputs=spec.get("sensitive_outputs"),
            config=cfg,
        )

    print("\n" + "=" * 64)
    print(f"run_id       : {t.run_id}")
    print(f"model        : {t.model}")
    print(f"status       : {t.final_status}")
    print(f"summary      : {t.final_summary}")
    print(f"outputs      : {t.final_outputs}")
    print(f"steps        : {len(t.steps)}")
    print(f"tokens       : {t.token_usage}")
    print(f"evidence     : evidence/discovery/{t.run_id}/")
    print("=" * 64)
    for s in t.steps:
        flag = " " if s.ok else "!"
        print(f" {flag}[{s.index:02d}] {s.tool_name:<14} {s.result[:80]}")
    return 0 if t.final_status in {"goal_reached", "business_outcome"} else 1


if __name__ == "__main__":
    sys.exit(main())
