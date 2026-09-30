"""Thin wrapper around a Playwright Chromium session.

One class, ``BrowserSession``, owns the browser/context/page lifecycle for both
the discovery loop and the replay engine. It is deliberately small: perception
(``cua.perception``) and acting (``cua.agent_tools`` / the replay engine) operate
on ``session.page`` directly.

The context is launched with ``--remote-debugging-port`` so Phase 8 (human
handoff) can attach a second client to the *same* live session over CDP.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright


@dataclass
class BrowserSession:
    base_url: str
    headless: bool = True
    # 0 == no debug port opened. Set to a real port to let a second, separate
    # process attach to this exact browser via
    # playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}") --
    # the escalation handoff in cua.replay uses this.
    cdp_port: int = 0
    slow_mo_ms: int = 0
    record_dir: Path | None = None

    _pw: Any = field(default=None, init=False, repr=False)
    _browser: Browser | None = field(default=None, init=False, repr=False)
    _context: BrowserContext | None = field(default=None, init=False, repr=False)
    page: Page | None = field(default=None, init=False, repr=False)

    def start(self) -> "BrowserSession":
        self._pw = sync_playwright().start()
        args = []
        if self.cdp_port:
            args.append(f"--remote-debugging-port={self.cdp_port}")
        self._browser = self._pw.chromium.launch(
            headless=self.headless, slow_mo=self.slow_mo_ms, args=args
        )
        ctx_kwargs: dict[str, Any] = {"viewport": {"width": 1280, "height": 900}}
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            ctx_kwargs["record_video_dir"] = str(self.record_dir)
        self._context = self._browser.new_context(**ctx_kwargs)
        self._context.set_default_timeout(8000)
        self.page = self._context.new_page()
        return self

    def resolve_url(self, path_or_url: str) -> str:
        """Join a relative path onto ``base_url``; an absolute URL passes through
        unchanged. Split out so a caller can check a target with the guardrails
        before ``goto`` actually requests it."""
        if "://" in path_or_url:
            return path_or_url
        return self.base_url.rstrip("/") + "/" + path_or_url.lstrip("/")

    def goto(self, path_or_url: str) -> None:
        self.page.goto(self.resolve_url(path_or_url), wait_until="domcontentloaded")

    def screenshot(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=True)
        return path

    def dom_snapshot(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.page.content(), encoding="utf-8")
        return path

    def close(self) -> None:
        for closer in (
            lambda: self._context and self._context.close(),
            lambda: self._browser and self._browser.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            with contextlib.suppress(Exception):
                closer()

    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()
