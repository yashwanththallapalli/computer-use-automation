"""Mock operator console.

Connects to the *same* live browser session a paused replay run is waiting
on (via CDP -- not a fresh browser) and resolves the escalation ticket. A real
operator console would be a web UI; this is the "bare/mock operator surface"
the brief explicitly allows, with the handoff mechanism itself -- the CDP
reconnect, the ticket, the resume -- kept real.

    python scripts/operator.py <ticket_id>                # review, then approve
    python scripts/operator.py <ticket_id> --reject
    python scripts/operator.py <ticket_id> --dismiss-notice --recover
"""

from __future__ import annotations

import argparse
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from playwright.sync_api import sync_playwright

from cua.escalation import load_ticket, resolve_ticket
from cua.perception import perceive


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticket_id")
    ap.add_argument("--reject", action="store_true", help="deny an approval_required ticket")
    ap.add_argument("--recover", action="store_true",
                    help="mark a 'stuck' ticket recovered (the default for that reason)")
    ap.add_argument("--dismiss-notice", action="store_true",
                    help="click through the unexpected_notice.html demo obstacle")
    ap.add_argument("--notes", default="", help="operator's note, recorded on the ticket")
    args = ap.parse_args(argv)

    ticket = load_ticket(args.ticket_id)
    print(f"ticket {ticket.ticket_id}  reason={ticket.reason}  trigger={ticket.trigger}")
    print(f"  capability : {ticket.capability_ref}")
    print(f"  goal       : {ticket.goal}")
    print(f"  step       : {ticket.step_id} -- {ticket.step_description}")
    print(f"  screenshot : {ticket.screenshot_path}")

    actions: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ticket.cdp_endpoint)
        page = browser.contexts[0].pages[0]
        print(f"  connected to live session at {ticket.cdp_endpoint} -> {page.url}")

        obs = perceive(page)
        print("--- what the operator sees on the live page ---")
        print(obs.to_prompt())
        print("---")

        if args.dismiss_notice:
            link = page.get_by_role("link", name="Dismiss and Continue")
            if link.count() == 1:
                link.click()
                page.wait_for_load_state("domcontentloaded")
                actions.append(f"clicked 'Dismiss and Continue' -> {page.url}")
                print(f"  dismissed notice, now at {page.url}")
            else:
                print("  no 'Dismiss and Continue' link found on this page")

        browser.close()  # disconnects this client; the paused replay's own
                         # connection is untouched -- it's a separate one

    if args.reject:
        resolution = "rejected"
    elif ticket.reason == "approval_required":
        resolution = "approved"
    else:
        resolution = "recovered"

    resolve_ticket(args.ticket_id, resolution, notes=args.notes, actions=actions)
    print(f"resolved {args.ticket_id} as {resolution!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
