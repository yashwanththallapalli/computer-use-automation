"""Resolve a Locator (ordered strategy chain) to a single element on the page.

Native strategies (role, text, label, placeholder, css, xpath, test_id) map
straight to Playwright locators. The two structural strategies (near_text,
table_cell) run a small DOM query that marks the matched element with
`data-cua-hit` so Playwright can pick it up.

`resolve()` walks the chain and returns the first strategy that matches exactly
one element, plus a tag like "near_text#0" so replay can log which one won
(anything past #0 is a drift signal).
"""

from __future__ import annotations

from playwright.sync_api import Error as PWError
from playwright.sync_api import Locator as PWLocator
from playwright.sync_api import Page

from .schema import Locator, LocatorKind

# Marks the element a structural query matched. Cleared before every query.
# Row/table lookups are scoped to *leaf* tables/rows (ones with no nested
# table) so a page-layout table that wraps the whole document never matches --
# textContent includes descendants, so it would otherwise "contain" anything.
_STRUCT_JS = r"""
({kind, value, opts}) => {
  document.querySelectorAll('[data-cua-hit]').forEach(e => e.removeAttribute('data-cua-hit'));
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const has = (el, t) => norm(el.textContent).includes(norm(t));
  const mark = el => { if (el) { el.setAttribute('data-cua-hit', '1'); return true; } return false; };

  const leafTables = () => [...document.querySelectorAll('table')].filter(t => !t.querySelector('table'));
  const rowsOf = table => [...table.querySelectorAll('tr')];

  if (kind === 'near_text') {
    if (opts.row_contains) {
      const target = opts.target_text || value;
      for (const table of leafTables()) {
        for (const tr of rowsOf(table)) {
          if (!has(tr, opts.row_contains)) continue;
          const hit = [...tr.querySelectorAll('a, button, input, select, textarea')]
            .find(el => norm(el.textContent) === norm(target) ||
                        norm(el.value) === norm(target) ||
                        norm(el.getAttribute('aria-label')) === norm(target));
          if (hit) return mark(hit);
        }
      }
      return false;
    }
    // a form control next to its visible label -- only leaf-table cells count
    // as labels, so a page-wide wrapper can never match as one.
    const anchor = opts.anchor || value;
    const cells = leafTables().flatMap(t => [...t.querySelectorAll('td, th')]);
    const labels = cells.filter(el => has(el, anchor) && !el.querySelector('input, select, textarea'));
    // a control if there is one (fill/select/check target); otherwise the
    // sibling cell itself, for reading a plain value next to its label
    for (const lab of labels) {
      const sib = lab.nextElementSibling;
      if (sib) {
        const inSib = sib.querySelector('input, select, textarea');
        if (inSib) return mark(inSib);
        return mark(sib);
      }
      const row = lab.closest('tr');
      const inRow = row && row.querySelector('input, select, textarea');
      if (inRow) return mark(inRow);
    }
    // fall back to non-table labels (label/span/p) for non-tabular markup
    const other = [...document.querySelectorAll('label, span, p')].filter(el => has(el, anchor));
    for (const lab of other) {
      const inSelf = lab.querySelector('input, select, textarea');
      if (inSelf) return mark(inSelf);
      const sib = lab.nextElementSibling;
      const inSib = sib && sib.querySelector('input, select, textarea');
      if (inSib) return mark(inSib);
    }
    return false;
  }

  if (kind === 'table_cell') {
    const col = opts.column || value;
    const rows = [].concat(opts.row_contains || []);
    const rowMatch = tr => rows.every(t => has(tr, t));
    for (const table of leafTables()) {
      const trs = rowsOf(table);
      // key/value grid: a row whose <th> is the column label
      for (const tr of trs) {
        const th = tr.querySelector('th');
        if (th && norm(th.textContent) === norm(col) && rowMatch(tr)) {
          const td = tr.querySelector('td');
          if (td) return mark(td);
        }
      }
      // data grid: header row gives the column index
      const head = table.querySelector('tr:has(th)');
      if (!head) continue;
      const idx = [...head.children].findIndex(h => norm(h.textContent) === norm(col));
      if (idx < 0) continue;
      for (const tr of trs) {
        if (tr === head || !rowMatch(tr)) continue;
        if (tr.children[idx]) return mark(tr.children[idx]);
      }
    }
    return false;
  }
  return false;
}
"""


class Resolver:
    def __init__(self, page: Page):
        self.page = page

    def resolve(self, locator: Locator, params: dict) -> tuple[PWLocator | None, str]:
        # substitute {param} placeholders through value *and* nested options
        # (e.g. row_contains: ["{member_id}"]) in one pass
        resolved = locator.resolved(params)
        for i, strat in enumerate(resolved.strategies):
            try:
                loc = self._one(strat)
            except PWError:
                continue
            if loc is None:
                continue
            try:
                if loc.count() == 1:
                    return loc, f"{strat.kind.value}#{i}"
            except PWError:
                continue
        return None, ""

    # -- per strategy ---------------------------------------------------

    def _one(self, strat) -> PWLocator | None:
        kind = strat.kind
        value = strat.value
        opts = strat.options

        if kind is LocatorKind.ROLE:
            name = opts.get("name")
            if name:
                return self.page.get_by_role(value, name=name,
                                             exact=bool(opts.get("exact")))
            return self.page.get_by_role(value)
        if kind is LocatorKind.TEXT:
            return self.page.get_by_text(value, exact=bool(opts.get("exact")))
        if kind is LocatorKind.LABEL:
            return self.page.get_by_label(value)
        if kind is LocatorKind.PLACEHOLDER:
            return self.page.get_by_placeholder(value)
        if kind is LocatorKind.ALT_TEXT:
            return self.page.get_by_alt_text(value)
        if kind is LocatorKind.TITLE:
            return self.page.get_by_title(value)
        if kind is LocatorKind.TEST_ID:
            return self.page.get_by_test_id(value)
        if kind is LocatorKind.CSS:
            return self.page.locator(value)
        if kind is LocatorKind.XPATH:
            return self.page.locator(f"xpath={value}")
        if kind in (LocatorKind.NEAR_TEXT, LocatorKind.TABLE_CELL):
            found = self.page.evaluate(
                _STRUCT_JS, {"kind": kind.value, "value": value, "opts": opts}
            )
            return self.page.locator('[data-cua-hit="1"]') if found else None
        return None
