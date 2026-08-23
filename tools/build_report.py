#!/usr/bin/env python3
"""Compile the Claude Design `.dc.html` source into a standalone static page.

The design ships in DC format: a `<x-dc>` template with `<helmet>`, `<sc-if>`
conditionals and a `DCLogic` props script, all rendered at runtime by
`support.js` + React. That is right for the design tool and wrong for a page we
want to serve, print, or open from disk with no JavaScript.

This script performs the transform once, reproducibly, so the design file stays
the single source of truth: re-run it whenever the design is re-imported.

    python3 tools/build_report.py

Transform:
  1. unwrap `<x-dc>`, hoist `<helmet>` contents into `<head>`
  2. resolve `<sc-if>` against the props defaults declared in the DC script
  3. normalise XHTML-isms (`<br></br>`, `<meta ...></meta>`) to HTML5
  4. deep-link `[n]` citations to their `#srcN` anchors (the design already
     carries the ids; the runtime template pointed every one at `#sources`)
  5. tag inline `grid-template-columns` patterns with classes so a stylesheet
     can make the fixed multi-column layouts responsive without editing the
     designer's inline styles
"""

from __future__ import annotations

import html
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "design" / "Disaster Readiness Research Report.dc.html"
OUT = ROOT / "report" / "index.html"

# Inline grid signatures -> class name. Keyed on the exact value the designer
# wrote so an unexpected new layout shows up as an unmapped warning rather than
# being silently mis-styled.
GRID_CLASSES = {
    "minmax(0,1fr) 300px": "rl-main-margin",
    "repeat(2,minmax(0,1fr))": "rl-cols-2",
    "repeat(4,minmax(0,1fr))": "rl-cols-4",
    "250px minmax(0,1fr) 250px": "rl-datarow",
    "1fr 28px 1fr 28px 1fr 28px 1fr": "rl-looprow",
    "88px minmax(0,1fr) 300px": "rl-phase",
    "44px 1fr": "rl-bullet",
    "18px 1fr": "rl-bullet",
}

RESPONSIVE_CSS = """
*,*::before,*::after{box-sizing:border-box}
html{scroll-behavior:smooth}
img,svg{max-width:100%;height:auto}
pre{overflow-x:auto}

/* The report is authored with inline styles, which outrank a stylesheet.
   These overrides are deliberately !important: they exist only to collapse
   fixed multi-column grids on narrow viewports, never to restyle the design. */
@media (max-width:1000px){
  .rl-cols-4{grid-template-columns:repeat(2,minmax(0,1fr))!important}
}
@media (max-width:900px){
  .rl-main-margin,.rl-datarow,.rl-phase{grid-template-columns:minmax(0,1fr)!important;gap:26px 0!important}
  .rl-phase>div:first-child{line-height:1!important}
  .rl-looprow{grid-template-columns:minmax(0,1fr)!important;gap:20px 0!important}
  .rl-looprow>div:nth-child(even){transform:rotate(90deg);padding:2px 0}
}
@media (max-width:760px){
  .rl-cols-2,.rl-cols-4{grid-template-columns:minmax(0,1fr)!important;gap:0!important}
  .rl-sources{columns:1!important}
  h1{font-size:clamp(40px,11vw,56px)!important}
  h2{font-size:31px!important}
}
@media (max-width:520px){
  header,section{padding-left:20px!important;padding-right:20px!important}
}

/* Citations are now real deep links; keep them unobtrusive in flowing text. */
a.rl-cite{white-space:nowrap;text-decoration:none;font-weight:600;font-size:0.92em}
a.rl-cite:hover{text-decoration:underline}
:target{scroll-margin-top:24px}

.rl-skip{position:absolute;left:-9999px;top:0;background:#1c1a16;color:#faf7f2;
  padding:10px 16px;z-index:10;font:600 13px/1 'Public Sans',sans-serif}
.rl-skip:focus{left:8px;top:8px;color:#faf7f2}

@media print{
  html,body{background:#fff}
  .rl-skip{display:none}
  section,header{page-break-inside:auto}
  aside,.rl-datarow{page-break-inside:avoid}
  a{color:#1c1a16!important;text-decoration:none}
}
"""


def fail(msg: str) -> None:
    print(f"build_report: {msg}", file=sys.stderr)
    raise SystemExit(1)


def extract(pattern: str, text: str, what: str) -> re.Match[str]:
    m = re.search(pattern, text, re.S)
    if not m:
        fail(f"could not locate {what} in {SRC.name}")
    return m


def prop_defaults(source: str) -> dict[str, object]:
    """Read the DC script's `data-props` blob for each prop's default value."""
    m = re.search(r'data-props="([^"]*)"', source)
    if not m:
        return {}
    spec = json.loads(html.unescape(m.group(1)))
    return {name: meta.get("default") for name, meta in spec.items()}


def resolve_sc_if(body: str, defaults: dict[str, object]) -> str:
    """Inline `<sc-if>` blocks: keep children when the bound prop is truthy."""
    pattern = re.compile(
        r'<sc-if\s+value="\{\{\s*(\w+)\s*\}\}"[^>]*>(.*?)</sc-if>', re.S
    )

    def sub(m: re.Match[str]) -> str:
        name, inner = m.group(1), m.group(2)
        if name not in defaults:
            fail(f"<sc-if> references unknown prop {name!r}")
        return inner if defaults[name] else ""

    body, n = pattern.subn(sub, body)
    if "<sc-if" in body:
        fail("unresolved <sc-if> remains after substitution")
    print(f"  resolved {n} <sc-if> blocks")
    return body


def normalise_html5(text: str) -> str:
    text = re.sub(r"<br\s*/?>\s*</br>", "<br>", text)
    text = re.sub(r"(<(?:meta|link)\b[^>]*?)\s*/?>\s*</(?:meta|link)>", r"\1>", text)
    return text


def link_citations(body: str) -> tuple[str, int]:
    """`<a href="#sources">[6][10]</a>` -> one anchor per source, each deep-linked.

    The design already emits `id="src6"` on every source entry; the runtime
    template just never pointed at them.
    """
    pattern = re.compile(r'<a href="#sources">((?:\[\d+\])+)</a>')
    count = 0

    def sub(m: re.Match[str]) -> str:
        nonlocal count
        refs = re.findall(r"\[(\d+)\]", m.group(1))
        count += len(refs)
        return "".join(
            f'<a class="rl-cite" href="#src{n}" '
            f'aria-label="Source {n}">[{n}]</a>' for n in refs
        )

    return pattern.sub(sub, body), count


def tag_grids(body: str) -> str:
    """Attach a class to every element carrying a known inline grid signature."""
    unmapped: set[str] = set()

    def sub(m: re.Match[str]) -> str:
        tag, style = m.group(1), m.group(2)
        gm = re.search(r"grid-template-columns:\s*([^;\"]+)", style)
        cls = None
        if gm:
            key = re.sub(r"\s+", " ", gm.group(1)).strip()
            cls = GRID_CLASSES.get(key)
            if cls is None:
                unmapped.add(key)
        elif re.search(r"\bcolumns:\s*2\b", style):
            cls = "rl-sources"
        if cls is None:
            return m.group(0)
        return f'<{tag} class="{cls}" style="{style}"'

    body = re.sub(r'<(\w+)\s+style="([^"]*)"', sub, body)
    for key in sorted(unmapped):
        print(f"  note: unmapped grid signature {key!r} (left as authored)")
    return body


def main() -> None:
    if not SRC.exists():
        fail(f"missing design source at {SRC}")
    source = SRC.read_text(encoding="utf-8")
    print(f"build_report: reading {SRC.name} ({len(source):,} bytes)")

    helmet = extract(r"<helmet>(.*?)</helmet>", source, "<helmet> block").group(1)
    body = extract(r"<x-dc>(.*?)</x-dc>", source, "<x-dc> template").group(1)
    body = body.replace(helmet, "", 1)
    body = re.sub(r"<helmet>.*?</helmet>", "", body, flags=re.S)

    defaults = prop_defaults(source)
    print(f"  props: {defaults}")
    body = resolve_sc_if(body, defaults)

    title_m = re.search(r"<title>(.*?)</title>", helmet, re.S)
    title = title_m.group(1).strip() if title_m else "The Readiness Loop"

    head = normalise_html5(helmet).strip()
    body = normalise_html5(body).strip()

    body, cites = link_citations(body)
    print(f"  deep-linked {cites} citations")
    body = tag_grids(body)

    description = (
        "Research briefing on an open-source agentic system that forecasts "
        "natural-disaster risk from public data, validates its own probabilities "
        "against history, and turns them into emergency plans."
    )

    # The viewport meta lives in the DC file's *outer* <head>, not in <helmet>
    # (the runtime hoists helmet into an existing document). Carry it over
    # explicitly, or the responsive rules below never fire on a phone.
    viewport = extract(
        r'(<meta\s+name="viewport"[^>]*>)', source, "viewport meta"
    ).group(1)

    out = f"""<!DOCTYPE html>
<html lang="en">
<head>
{normalise_html5(viewport)}
{head}
<meta name="description" content="{description}">
<meta name="color-scheme" content="light">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{description}">
<meta property="og:type" content="article">
<style>{RESPONSIVE_CSS}</style>
</head>
<body>
<a class="rl-skip" href="#roadmap">Skip to the roadmap</a>
{body}
</body>
</html>
"""

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(out, encoding="utf-8")
    print(f"build_report: wrote {OUT.relative_to(ROOT)} ({len(out):,} bytes)")


if __name__ == "__main__":
    main()
