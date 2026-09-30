"""Replay a saved capability artifact -- no LLM involved.

    python scripts/replay.py open_subaccount member_id=101445 account_type=Savings initial_deposit=250.00 member_authorized=yes --approve
    python scripts/replay.py read_member_balance member_id=100234 account_type=Savings
    python scripts/replay.py open_subaccount member_id=999999 account_type=Savings initial_deposit=250.00 member_authorized=yes --approve

Escalation demo (needs a second terminal running scripts/operator.py):
    python scripts/replay.py open_subaccount member_id=101445 account_type=Savings initial_deposit=250.00 member_authorized=yes --escalate
    python scripts/replay.py read_member_balance member_id=100234 account_type=Savings --escalate --unexpected-notice
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from cua._appserver import running_bankapp
from cua.replay import ReplayOptions, replay
from cua.schema import CapabilityArtifact

CAP_DIR = Path("cua/capabilities")


def _announce_escalation(ticket) -> None:
    print("\n" + "!" * 64)
    print(f"! ESCALATED  ticket={ticket.ticket_id}  reason={ticket.reason}")
    print(f"! {ticket.step_description or ticket.trigger}")
    print(f"! Run in another terminal:")
    print(f"!   python scripts/operator.py {ticket.ticket_id}"
          + ("  --dismiss-notice" if ticket.reason == "stuck" else ""))
    print("!" * 64 + "\n", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("capability")
    ap.add_argument("params", nargs="*", help="name=value pairs")
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--approve", action="store_true", help="approve risky/confirm steps")
    ap.add_argument("--no-headless", action="store_true")
    ap.add_argument("--interstitial", action="store_true")
    ap.add_argument("--unexpected-notice", action="store_true",
                    help="inject the demo obstacle no taxonomy rule recognises")
    ap.add_argument("--escalate", action="store_true",
                    help="pause and raise a HandoffTicket instead of failing fast")
    ap.add_argument("--escalation-timeout", type=float, default=120.0)
    args = ap.parse_args(argv)

    inputs = dict(p.split("=", 1) for p in args.params)
    art = CapabilityArtifact.load(CAP_DIR / f"{args.capability}.v{args.version}.json")

    env_extra = {}
    if args.interstitial:
        env_extra["BANKAPP_INTERSTITIAL"] = "1"
    if args.unexpected_notice:
        env_extra["BANKAPP_UNEXPECTED_NOTICE"] = "1"

    opts = ReplayOptions(
        approve_risky=args.approve, headless=not args.no_headless,
        escalate=args.escalate, escalation_timeout_s=args.escalation_timeout,
        on_escalate=_announce_escalation if args.escalate else None,
    )

    with running_bankapp(base_url=art.target.base_url, env_extra=env_extra):
        result = replay(art, inputs, opts)

    print("\n" + "=" * 64)
    print(f"status  : {result.status.value}")
    if result.status.value == "success":
        print(f"outputs : {result.outputs}")
    elif result.status.value == "business_outcome":
        print(f"code    : {result.outcome_code}")
        print(f"message : {result.message}")
        print(f"escalated: {result.escalated}")
    else:
        print(f"code    : {result.failure_code}")
        print(f"step    : {result.failed_step_id}")
        print(f"expected: {result.expected}")
        print(f"observed: {result.observed}")
        if result.screenshot_path:
            print(f"screenshot: {result.screenshot_path}")
    print(f"steps   : {result.context.steps_executed}")
    print(f"evidence: {result.context.evidence_dir}")
    if result.context.escalations:
        print(f"escalations: {result.context.escalations}")
    print("=" * 64)
    for t in result.context.trace:
        print(f"  [{t.step_id:24}] {t.action:9} {t.duration_ms:5}ms  "
              f"locator={t.locator_used}")
    return 0 if result.status.value in {"success", "business_outcome"} else 1


if __name__ == "__main__":
    sys.exit(main())
