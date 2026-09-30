"""Flask app factory for the mock bank back-office tool.

Routes (all server-rendered HTML, table-based, no test IDs):

    GET  /                                          -> redirect to search
    GET  /members/search?q=<term>                   -> search form + results
    GET  /members/<member_number>                   -> member detail
    GET  /members/<member_number>/sub-accounts/new  -> open-sub-account form
    POST /members/<member_number>/sub-accounts      -> create (or re-render w/ errors)
    GET  /members/<member_number>/sub-accounts/<sub_account_number>/confirmation
    POST /admin/reset                               -> restore seed data (JSON)

Business/error surfaces the automation must handle:
  - member-not-found: search with no matches renders a distinct panel (HTTP 200)
  - validation-error: bad form input re-renders the form with an error banner (HTTP 200)
  - permission-denied: opening a sub-account for a non-Active member is refused (HTTP 200)
  - recoverable interstitial: with BANKAPP_INTERSTITIAL=1, member detail is gated
    by a "system notice" page that must be dismissed with Continue
  - transient slowness: BANKAPP_LATENCY_MS delays every response
  - unmodeled obstacle: with BANKAPP_UNEXPECTED_NOTICE=1, member detail is
    gated by a page no capability's outcome_taxonomy knows about -- for
    demonstrating human escalation (a genuine "stuck" case, not a known
    recoverable one), not something replay should ever auto-handle

Config via environment (all optional):
  BANKAPP_INTERSTITIAL=1        enable the recoverable interstitial on member detail
  BANKAPP_LATENCY_MS=<int>      artificial per-request latency in milliseconds
  BANKAPP_UNEXPECTED_NOTICE=1   enable the unmodeled obstacle on member detail
  BANKAPP_TENANT=<id>           serve a differently-configured tenant build (see
                                 _TENANTS below) instead of the base Meridian one
"""

from __future__ import annotations

import os
import time

from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    url_for,
    jsonify,
)

from .data import (
    ACCOUNT_TYPES,
    region_for,
    store,
    sub_account_permission,
    validate_new_sub_account,
)

# Same product, configured differently per institution -- the thing the
# heterogeneity story in REPORT.md needs a real second instance of. None is
# the vendor-generic base build; every other key is a tenant that runs the
# same routes and the same underlying data but looks and labels things
# differently, which is exactly what forces a capability artifact recorded
# against one to need a TenantOverlay before it replays cleanly on another.
_TENANTS: dict[str | None, dict] = {
    None: {
        "BANK_NAME": "MERIDIAN CREDIT UNION",
        "BANNER_META": "Teller: OP-4471 | Branch: Downtown",
        "FOOTER_ORG": "Meridian CU",
        "FOOTER_VERSION": "v3.11.2",
        "OPEN_LINK_TEXT": "Open",
        "SHOW_REGION_COLUMN": False,
    },
    "harborview": {
        "BANK_NAME": "HARBORVIEW FEDERAL CREDIT UNION",
        "BANNER_META": "Teller: HV-2209 | Branch: Lakeside",
        "FOOTER_ORG": "Harborview FCU",
        "FOOTER_VERSION": "v2.8.4",
        # same "open this member's sub-accounts" action, relabeled, and an
        # extra leading column -- enough to break a locator chain recorded
        # against the base build without changing what the flow does
        "OPEN_LINK_TEXT": "View Details",
        "SHOW_REGION_COLUMN": True,
    },
}


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["INTERSTITIAL"] = os.environ.get("BANKAPP_INTERSTITIAL") == "1"
    app.config["LATENCY_MS"] = int(os.environ.get("BANKAPP_LATENCY_MS", "0") or "0")
    app.config["UNEXPECTED_NOTICE"] = os.environ.get("BANKAPP_UNEXPECTED_NOTICE") == "1"

    tenant = os.environ.get("BANKAPP_TENANT") or None
    if tenant not in _TENANTS:
        raise ValueError(
            f"unknown BANKAPP_TENANT {tenant!r}; known: {sorted(t for t in _TENANTS if t)}"
        )
    app.config.update(_TENANTS[tenant])
    app.config["TENANT_ID"] = tenant
    app.jinja_env.globals["region_for"] = region_for

    @app.before_request
    def _inject_latency():
        if app.config["LATENCY_MS"] > 0:
            time.sleep(app.config["LATENCY_MS"] / 1000.0)

    @app.get("/")
    def index():
        return redirect(url_for("search"))

    @app.get("/members/search")
    def search():
        q = request.args.get("q", "")
        searched = "q" in request.args
        results = store.search(q) if searched else []
        not_found = searched and q.strip() != "" and len(results) == 0
        return render_template(
            "search.html",
            q=q,
            searched=searched,
            results=results,
            not_found=not_found,
        )

    @app.get("/members/<member_number>")
    def member_detail(member_number):
        member = store.get_member(member_number)
        if member is None:
            abort(404)
        if app.config["INTERSTITIAL"] and request.args.get("ack") != "1":
            # recoverable condition: a known interstitial the replay engine
            # must recognise and dismiss before it can read the page.
            return render_template(
                "interstitial.html",
                continue_url=url_for(
                    "member_detail", member_number=member_number, ack="1"
                ),
            )
        if app.config["UNEXPECTED_NOTICE"] and request.args.get("notice_dismissed") != "1":
            # deliberately NOT in any artifact's outcome_taxonomy -- replay
            # can't auto-recover from this, only a human dismissing it can.
            return render_template(
                "unexpected_notice.html",
                dismiss_url=url_for(
                    "member_detail", member_number=member_number, notice_dismissed="1"
                ),
            )
        return render_template("member_detail.html", member=member)

    @app.get("/members/<member_number>/sub-accounts/new")
    def new_sub_account(member_number):
        member = store.get_member(member_number)
        if member is None:
            abort(404)
        allowed, reason = sub_account_permission(member)
        if not allowed:
            return render_template(
                "permission_denied.html", member=member, reason=reason
            )
        return render_template(
            "sub_account_new.html",
            member=member,
            account_types=ACCOUNT_TYPES,
            errors=[],
            form={},
        )

    @app.post("/members/<member_number>/sub-accounts")
    def create_sub_account(member_number):
        member = store.get_member(member_number)
        if member is None:
            abort(404)

        allowed, reason = sub_account_permission(member)
        if not allowed:
            return render_template(
                "permission_denied.html", member=member, reason=reason
            )

        form = request.form.to_dict()
        cleaned, errors = validate_new_sub_account(form)
        if errors:
            # validation-error surface: re-render form, HTTP 200, error banner
            return (
                render_template(
                    "sub_account_new.html",
                    member=member,
                    account_types=ACCOUNT_TYPES,
                    errors=errors,
                    form=form,
                ),
                200,
            )

        record = store.add_sub_account(
            member_number, cleaned["account_type"], cleaned["initial_deposit"]
        )
        return redirect(
            url_for(
                "sub_account_confirmation",
                member_number=member_number,
                sub_account_number=record["sub_account_number"],
            )
        )

    @app.get(
        "/members/<member_number>/sub-accounts/<sub_account_number>/confirmation"
    )
    def sub_account_confirmation(member_number, sub_account_number):
        member = store.get_member(member_number)
        if member is None:
            abort(404)
        record = next(
            (
                sa
                for sa in member["sub_accounts"]
                if sa["sub_account_number"] == sub_account_number
            ),
            None,
        )
        if record is None:
            abort(404)
        return render_template(
            "sub_account_confirm.html", member=member, record=record
        )

    @app.post("/admin/reset")
    def admin_reset():
        store.reset()
        return jsonify({"status": "reset"})

    @app.errorhandler(404)
    def not_found(err):
        return render_template("not_found.html"), 404

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
