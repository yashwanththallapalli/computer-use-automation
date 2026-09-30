"""Turns the live page into a compact text observation for the LLM.

The discovery agent never sees pixels. What it gets each turn is an
``Observation``: a page summary (url, title, headings, any alert/notice/error
text so business outcomes actually get noticed), a trimmed digest of the main
content, and a numbered list of actionable elements. Each element carries its
role, its accessible name, and the visible label sitting near it (``near``),
since that's what a generated locator ends up anchoring on.

Every actionable element also gets stamped with ``data-cua-ref="<n>"`` so the
act layer can find the same element again within the same turn. That's just a
runtime bookkeeping detail, though — the actual locators saved into an
artifact come from the richer metadata captured here, not from the ref.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from playwright.sync_api import Page

# JS executed in the page. Stamps data-cua-ref and returns a structured snapshot.
_SNAPSHOT_JS = r"""
() => {
  const MAX_TEXT = 2500;

  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' &&
           s.display !== 'none' && s.opacity !== '0';
  };

  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();

  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'submit' || t === 'button' || t === 'reset') return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      return 'textbox';
    }
    return tag;
  };

  const labelFor = (el) => {
    // 1. aria-label / aria-labelledby
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const n = document.getElementById(lb);
      if (n) return clean(n.textContent);
    }
    // 2. <label for=id> or wrapping <label>
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return clean(l.textContent);
    }
    const wrap = el.closest('label');
    if (wrap) return clean(wrap.textContent);
    return '';
  };

  const accName = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && (type === 'submit' || type === 'button'))
      return clean(el.value);
    if (tag === 'button' || tag === 'a') return clean(el.textContent) || clean(el.value);
    return labelFor(el) || clean(el.getAttribute('placeholder')) ||
           clean(el.getAttribute('title')) || '';
  };

  // The visible text a human reads as "the label of this control": the label,
  // else the nearest preceding table cell / sibling text in the same row.
  const nearText = (el) => {
    const l = labelFor(el);
    if (l) return l;
    const cell = el.closest('td, th, li, p, div');
    if (cell) {
      // preceding sibling cell in a table row (this app's layout)
      let prev = cell.previousElementSibling;
      while (prev) {
        const t = clean(prev.textContent);
        if (t) return t;
        prev = prev.previousElementSibling;
      }
      // text directly before the control inside its container
      const ownText = clean(cell.textContent).replace(clean(el.textContent), '');
      if (ownText) return ownText.slice(0, 80);
    }
    return '';
  };

  const cssPath = (el) => {
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 6) {
      let sel = node.tagName.toLowerCase();
      if (node.id) { parts.unshift(`${sel}#${CSS.escape(node.id)}`); break; }
      const name = node.getAttribute('name');
      if (name) sel += `[name="${name}"]`;
      const parent = node.parentElement;
      if (parent) {
        const sibs = [...parent.children].filter(c => c.tagName === node.tagName);
        if (sibs.length > 1) sel += `:nth-of-type(${sibs.indexOf(node) + 1})`;
      }
      parts.unshift(sel);
      node = node.parentElement;
    }
    return parts.join(' > ');
  };

  // --- page-level ------------------------------------------------------
  const headings = [...document.querySelectorAll('h1,h2,h3')]
    .map(h => clean(h.textContent)).filter(Boolean);

  const banners = [...document.querySelectorAll(
      '[role=alert], .panel, .notice, .error, .success, .flash')]
    .map(b => clean(b.textContent)).filter(Boolean);

  let main = null;
  for (const s of ['td.content', 'main', '.content', 'body']) {
    main = document.querySelector(s);
    if (main) break;
  }
  let digest = '';
  if (main) {
    const rows = [...main.querySelectorAll('tr')];
    if (rows.length) {
      digest = rows.map(tr => [...tr.children]
        .map(c => clean(c.textContent)).filter(Boolean).join(' | '))
        .filter(Boolean).join('\n');
    } else {
      digest = clean(main.textContent);
    }
  }
  digest = digest.slice(0, MAX_TEXT);

  const columnHeader = (td) => {
    const table = td.closest('table');
    const row = td.closest('tr');
    if (!table || !row) return '';
    // key/value grid: the row has its own <th> label -> that's the "column"
    const ownTh = row.querySelector('th');
    if (ownTh && ownTh !== td) return clean(ownTh.textContent);
    // data grid: match by column index against a separate header row
    const idx = [...row.children].indexOf(td);
    const headRow = table.querySelector('tr:has(th)');
    if (headRow && headRow !== row && headRow.children[idx]) {
      return clean(headRow.children[idx].textContent);
    }
    return '';
  };

  // --- actionable elements -------------------------------------------
  const sel = 'a[href], button, input, select, textarea, [role=button], [onclick]';
  const els = [...document.querySelectorAll(sel)].filter(isVisible);
  const elements = [];
  els.forEach((el, i) => {
    el.setAttribute('data-cua-ref', String(i));
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const rec = {
      ref: i,
      role: roleOf(el),
      name: accName(el).slice(0, 120),
      near: nearText(el).slice(0, 120),
      tag,
      type: type || null,
      name_attr: el.getAttribute('name') || null,
      id: el.id || null,
      placeholder: el.getAttribute('placeholder') || null,
      aria_label: el.getAttribute('aria-label') || null,
      href: tag === 'a' ? el.getAttribute('href') : null,
      value: (tag === 'input' && type !== 'password') ? (el.value || null) : null,
      checked: (type === 'checkbox' || type === 'radio') ? el.checked : null,
      disabled: !!el.disabled,
      options: tag === 'select'
        ? [...el.options].map(o => clean(o.textContent)).filter(Boolean) : null,
      css_path: cssPath(el),
      row_text: el.closest('tr') ? clean(el.closest('tr').textContent).slice(0, 200) : null,
    };
    elements.push(rec);
  });

  // --- readable data cells (grids) -- targets for extract_value ------
  let ref = elements.length;
  const readables = [];
  const seenTd = new Set();
  [...document.querySelectorAll('table.grid td')].forEach((td) => {
    if (!isVisible(td)) return;
    const text = clean(td.textContent);
    if (!text || td.querySelector('a[href],button,input,select,textarea')) return;
    if (seenTd.has(td)) return;
    seenTd.add(td);
    td.setAttribute('data-cua-ref', String(ref));
    readables.push({
      ref,
      role: 'cell',
      name: text.slice(0, 120),
      column: columnHeader(td),
      row_text: td.closest('tr') ? clean(td.closest('tr').textContent).slice(0, 200) : '',
      css_path: cssPath(td),
    });
    ref += 1;
  });

  return {
    url: location.href,
    title: document.title,
    headings, banners, digest, elements, readables,
  };
}
"""


class ElementInfo(BaseModel):
    ref: int
    role: str
    name: str = ""
    near: str = ""
    tag: str = ""
    type: str | None = None
    name_attr: str | None = None
    id: str | None = None
    placeholder: str | None = None
    aria_label: str | None = None
    href: str | None = None
    value: str | None = None
    checked: bool | None = None
    disabled: bool = False
    options: list[str] | None = None
    css_path: str = ""
    row_text: str | None = None

    def one_line(self) -> str:
        bits = [f"[{self.ref}]", self.role]
        if self.name:
            bits.append(f'"{self.name}"')
        if self.row_text and self.tag == "a":
            bits.append(f"(in row: \"{self.row_text[:90]}\")")
        elif self.near and self.near.lower() not in self.name.lower():
            bits.append(f"(near: \"{self.near}\")")
        if self.options:
            bits.append("options=" + "/".join(self.options))
        if self.checked is not None:
            bits.append(f"checked={self.checked}")
        if self.disabled:
            bits.append("DISABLED")
        return " ".join(bits)


class ReadableCell(BaseModel):
    ref: int
    role: str = "cell"
    name: str = ""             # the cell's text value
    column: str = ""           # the column / row header it sits under
    row_text: str = ""
    css_path: str = ""

    def one_line(self) -> str:
        bits = [f"[{self.ref}]", "value", f'"{self.name}"']
        if self.column:
            bits.append(f"(column: \"{self.column}\")")
        if self.row_text and self.row_text != self.name:
            bits.append(f"(row: \"{self.row_text[:90]}\")")
        return " ".join(bits)


class Observation(BaseModel):
    url: str
    title: str
    headings: list[str] = []
    banners: list[str] = []
    digest: str = ""
    elements: list[ElementInfo] = []
    readables: list[ReadableCell] = []

    def to_prompt(self) -> str:
        lines = [f"URL: {self.url}", f"TITLE: {self.title}"]
        if self.headings:
            lines.append("HEADINGS: " + " / ".join(self.headings))
        if self.banners:
            lines.append("BANNERS / MESSAGES:")
            lines += [f"  - {b}" for b in self.banners]
        lines.append("")
        lines.append("PAGE CONTENT:")
        lines.append(self.digest or "(no text content)")
        lines.append("")
        lines.append("ACTIONABLE ELEMENTS (use the [n] ref in tool calls):")
        lines += ([f"  {e.one_line()}" for e in self.elements] or ["  (none)"])
        if self.readables:
            lines.append("")
            lines.append("READABLE VALUES (use the [n] ref with extract_value):")
            lines += [f"  {r.one_line()}" for r in self.readables]
        return "\n".join(lines)

    def element(self, ref: int) -> ElementInfo:
        for e in self.elements:
            if e.ref == ref:
                return e
        raise KeyError(f"no actionable element ref {ref} in current observation")

    def readable(self, ref: int) -> ReadableCell:
        for r in self.readables:
            if r.ref == ref:
                return r
        raise KeyError(f"no readable ref {ref} in current observation")

    def any_ref(self, ref: int):
        try:
            return self.element(ref)
        except KeyError:
            return self.readable(ref)


def perceive(page: Page) -> Observation:
    page.wait_for_load_state("domcontentloaded")
    raw: dict[str, Any] = page.evaluate(_SNAPSHOT_JS)
    return Observation.model_validate(raw)
