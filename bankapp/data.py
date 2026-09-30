"""In-memory data store for the mock bank app.

State lives in a module-level ``Store`` instance. ``Store.reset()`` restores the
seed data verbatim, which every discovery/replay run calls first so it starts
from an identical known state (this is what makes deterministic replay testable).
"""

from __future__ import annotations

import copy
import threading
from decimal import Decimal, InvalidOperation

# Account types offered by the "open sub-account" form. Order matters: it is the
# render order of the <select> options.
ACCOUNT_TYPES = [
    "Savings",
    "Checking",
    "Money Market",
    "Certificate of Deposit",
]

MIN_OPENING_DEPOSIT = Decimal("25.00")

# Branch -> region, for the tenant variant that adds a Region column to
# search results. Not every branch needs an entry; an unmapped one just
# falls back to "Other" rather than raising.
_REGIONS = {
    "Downtown": "Central",
    "Northgate": "North",
    "Westpark": "West",
}


def region_for(branch: str) -> str:
    return _REGIONS.get(branch, "Other")


def _seed_members():
    return {
        "100234": {
            "member_number": "100234",
            "name": "Alice Thornton",
            "dob": "1985-03-12",
            "email": "alice.thornton@example.com",
            "phone": "555-0142",
            "status": "Active",
            "branch": "Downtown",
            "member_since": "2012-04-18",
            "sub_accounts": [
                {
                    "sub_account_number": "100234-01",
                    "type": "Savings",
                    "opened_on": "2012-04-18",
                    "balance": "4210.55",
                    "status": "Open",
                },
                {
                    "sub_account_number": "100234-02",
                    "type": "Checking",
                    "opened_on": "2015-09-02",
                    "balance": "1875.10",
                    "status": "Open",
                },
            ],
        },
        "100987": {
            "member_number": "100987",
            "name": "Robert Chen",
            "dob": "1979-11-30",
            "email": "robert.chen@example.com",
            "phone": "555-0198",
            "status": "Dormant",
            "branch": "Northgate",
            "member_since": "2008-01-22",
            "sub_accounts": [
                {
                    "sub_account_number": "100987-01",
                    "type": "Checking",
                    "opened_on": "2008-01-22",
                    "balance": "620.00",
                    "status": "Open",
                },
            ],
        },
        "101445": {
            "member_number": "101445",
            "name": "Maria Gonzalez",
            "dob": "1992-07-08",
            "email": "maria.gonzalez@example.com",
            "phone": "555-0231",
            "status": "Active",
            "branch": "Downtown",
            "member_since": "2019-06-14",
            "sub_accounts": [],
        },
        "102310": {
            "member_number": "102310",
            "name": "James Thornton",
            "dob": "1965-02-19",
            "email": "james.thornton@example.com",
            "phone": "555-0277",
            "status": "Active",
            "branch": "Westpark",
            "member_since": "2001-10-05",
            "sub_accounts": [
                {
                    "sub_account_number": "102310-01",
                    "type": "Savings",
                    "opened_on": "2001-10-05",
                    "balance": "15320.87",
                    "status": "Open",
                },
                {
                    "sub_account_number": "102310-03",
                    "type": "Money Market",
                    "opened_on": "2011-03-11",
                    "balance": "42000.00",
                    "status": "Open",
                },
            ],
        },
    }


class Store:
    """Thread-safe-ish in-memory store. Flask's dev server is single-threaded by
    default, but the reset/mutate paths take a lock so tests can hit it freely."""

    def __init__(self):
        self._lock = threading.Lock()
        self._members = _seed_members()

    def reset(self):
        with self._lock:
            self._members = _seed_members()

    # --- reads -----------------------------------------------------------------

    def search(self, query: str):
        """Case-insensitive partial name match, or exact member-number match.
        Empty query returns nothing (forces the user to actually search)."""
        q = (query or "").strip().lower()
        if not q:
            return []
        results = []
        for m in self._members.values():
            if q == m["member_number"].lower() or q in m["name"].lower():
                results.append(copy.deepcopy(m))
        return sorted(results, key=lambda m: m["name"])

    def get_member(self, member_number: str):
        m = self._members.get(str(member_number))
        return copy.deepcopy(m) if m else None

    # --- writes --------------------------------------------------------------

    def add_sub_account(self, member_number: str, account_type: str, initial_deposit: Decimal):
        """Append a new sub-account and return its record. Caller has already
        validated inputs."""
        with self._lock:
            m = self._members.get(str(member_number))
            if m is None:
                raise KeyError(member_number)
            existing_suffixes = [
                int(sa["sub_account_number"].split("-")[1])
                for sa in m["sub_accounts"]
                if "-" in sa["sub_account_number"]
            ]
            next_suffix = (max(existing_suffixes) + 1) if existing_suffixes else 1
            record = {
                "sub_account_number": f"{member_number}-{next_suffix:02d}",
                "type": account_type,
                "opened_on": "2026-09-09",
                "balance": f"{initial_deposit:.2f}",
                "status": "Open",
            }
            m["sub_accounts"].append(record)
            return copy.deepcopy(record)


def sub_account_permission(member):
    """Business rule: a sub-account may only be opened for a member in good
    standing. A Dormant (or Closed) member requires a supervisor override, which
    the automation cannot perform on its own -- replay surfaces this as a
    business outcome and the agent loop treats it as an escalation trigger.

    Returns (allowed: bool, reason: str | None)."""
    status = (member or {}).get("status", "")
    if status == "Active":
        return True, None
    return False, (
        f"Member status is '{status}'. Opening a sub-account for a non-active "
        f"member requires a supervisor override."
    )


def validate_new_sub_account(form):
    """Validate the open-sub-account form dict. Returns (cleaned, errors) where
    cleaned has a normalized Decimal ``initial_deposit`` and ``errors`` is a list
    of human-readable strings (empty == valid)."""
    errors = []
    account_type = (form.get("account_type") or "").strip()
    raw_deposit = (form.get("initial_deposit") or "").strip()
    authorized = form.get("authorization_confirmed") in ("on", "true", "1", "yes")

    if not account_type:
        errors.append("Account type is required.")
    elif account_type not in ACCOUNT_TYPES:
        errors.append("Account type is not recognized.")

    deposit_value = None
    if not raw_deposit:
        errors.append("Initial deposit is required.")
    else:
        try:
            deposit_value = Decimal(raw_deposit.replace("$", "").replace(",", ""))
        except (InvalidOperation, ValueError):
            errors.append("Initial deposit must be a valid dollar amount.")
        else:
            if deposit_value < MIN_OPENING_DEPOSIT:
                errors.append(
                    f"Minimum opening deposit is ${MIN_OPENING_DEPOSIT:.2f}."
                )

    if not authorized:
        errors.append("You must confirm the member authorized this action.")

    cleaned = {"account_type": account_type, "initial_deposit": deposit_value}
    return cleaned, errors


store = Store()
