"""Mock legacy bank back-office application.

A small server-rendered Flask app that stands in for a legacy back-office tool
with no API: table-based HTML, no test IDs, in-memory state. It exists so the
discovery agent and replay engine have a realistic target with genuine business
outcomes (member-not-found) and validation errors to detect.
"""

from .app import create_app

__all__ = ["create_app"]
