#!/usr/bin/env python3
"""Regenerate docs/media: screenshots and recordings of the loop, with Playwright.

What this does, in order:

1. Runs the real `readiness` CLI, command by command, against a *scratch* copy
   of the contract registry and an *empty* scratch experiments tree — so the
   demo registers a contract and runs the loop without touching anything that
   is committed. Each command's output is saved verbatim under
   `docs/media/transcripts/` and rendered in a terminal frame that a headless
   Chromium screenshots.
2. Renders the committed example ledgers with `readiness dashboard` and
   screenshots the pages and their reliability diagrams.
3. Screenshots the research report.
4. Replays the loop's transcript line by line and assembles the frames into an
   animated GIF — a recording of a real run, not a mock-up.

Requirements: the pinned data for the demo contract in `snapshots/` (run
`readiness snapshot -c tornado-ok` once), and

    pip install -e '.[docs]'
    playwright install chromium

Nothing here is imported by the package; it is a documentation tool.
"""

from __future__ import annotations

import html
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
MEDIA = ROOT / "docs" / "media"
TRANSCRIPTS = MEDIA / "transcripts"
DEMO_CONTRACT = "tornado-ok"
NEW_CONTRACT = ("hail-ks", "--hazard", "hail", "--state", "KS",
                "--description", "Damaging hail, Kansas counties, quarterly.")
EXAMPLES = ("inland-flood-la", "tornado-ok", "tropical-cyclone-gulf")

#: (slug, argv after `readiness`) — the walkthrough, in order.
COMMANDS: list[tuple[str, list[str]]] = [
    ("contracts", ["contracts"]),
    ("contract", ["contract", "-c", DEMO_CONTRACT]),
    ("hazards", ["hazards"]),
    ("register", ["register", *NEW_CONTRACT]),
    ("panel", ["panel", "-c", DEMO_CONTRACT, "--quiet"]),
    ("score", ["score", "climatology-seasonal", "-c", DEMO_CONTRACT, "--quiet"]),
    ("loop", ["loop", "-c", DEMO_CONTRACT, "--quiet"]),
    ("ledger", ["ledger", "-c", DEMO_CONTRACT]),
    ("verify", ["verify", "-c", DEMO_CONTRACT, "--quiet"]),
    ("canary", ["canary", "-c", DEMO_CONTRACT, "--quiet"]),
]

TERMINAL_CSS = """
body{margin:0;background:#12151c;padding:22px}
.frame{background:#1b1f28;border-radius:10px;box-shadow:0 12px 40px rgba(0,0,0,.45);
  overflow:hidden;width:960px}
.bar{display:flex;align-items:center;gap:8px;padding:10px 14px;background:#262b36}
.bar i{width:12px;height:12px;border-radius:50%;display:inline-block}
.bar .t{margin-left:10px;color:#9aa3b2;font:12px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
pre{margin:0;padding:16px 18px;color:#e6e9ef;
  font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap}
.prompt{color:#7ee787}.cmd{color:#fff;font-weight:600}
.ok{color:#7ee787}.fail{color:#ff7b72}.rej{color:#d2a8ff}.warn{color:#ffa657}.dim{color:#8b949e}
"""


def fail(msg: str) -> None:
    print(f"capture: {msg}", file=sys.stderr)
    raise SystemExit(1)


def colourise(text: str) -> str:
    """Escape, then tint the few tokens that carry meaning in the CLI's output."""
    out = []
    for line in text.split("\n"):
        e = html.escape(line)
        if line.startswith("$ "):
            e = f'<span class="prompt">$</span> <span class="cmd">{html.escape(line[2:])}</span>'
        else:
            for token, cls in (("[ok]", "ok"), ("[PASS]", "ok"), ("PASS", "ok"),
                               ("[FAIL]", "fail"), ("FAIL", "fail"),
                               ("REJECTED", "rej"), ("TRIPPED", "rej"),
                               ("WARNING", "warn"), ("clear]", "dim")):
                e = e.replace(token, f'<span class="{cls}">{token}</span>', 1)
        out.append(e)
    return "\n".join(out)


def terminal_html(title: str, text: str) -> str:
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>{TERMINAL_CSS}</style>"
        f"</head><body><div class='frame'><div class='bar'>"
        "<i style='background:#ff5f57'></i><i style='background:#febc2e'></i>"
        f"<i style='background:#28c840'></i><span class='t'>{html.escape(title)}</span>"
        f"</div><pre>{colourise(text)}</pre></div></body></html>"
    )


def run_cli(argv: list[str], env: dict) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "readiness.cli", *argv],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout + (proc.stderr if proc.returncode else "")


def main() -> None:
    try:
        from PIL import Image
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        fail(f"{exc}. Install with: pip install -e '.[docs]' && playwright install chromium")

    if not any((ROOT / "snapshots" / "storm_events").glob("40_*.jsonl")):
        fail(f"no pinned data for {DEMO_CONTRACT}; run `readiness snapshot -c {DEMO_CONTRACT}`")

    MEDIA.mkdir(parents=True, exist_ok=True)
    TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    # Scratch space lives inside the repo (and is gitignored) so the paths the
    # CLI prints in the transcripts are short and obviously not the real
    # registry or ledgers.
    scratch = ROOT / "docs" / ".demo"
    shutil.rmtree(scratch, ignore_errors=True)
    shutil.copytree(ROOT / "contracts", scratch / "contracts",
                    ignore=shutil.ignore_patterns("README.md"))
    env = {
        **os.environ,
        "READINESS_CONTRACTS_DIR": str(scratch / "contracts"),
        "READINESS_EXPERIMENTS_DIR": str(scratch / "experiments"),
        "PYTHONUNBUFFERED": "1",
    }

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1004, "height": 700}, device_scale_factor=1.5)

        # 1. the walkthrough commands ---------------------------------------
        transcripts: dict[str, str] = {}
        for slug, argv in COMMANDS:
            code, out = run_cli(argv, env)
            text = f"$ readiness {' '.join(argv)}\n{out.rstrip()}\n"
            transcripts[slug] = text
            (TRANSCRIPTS / f"{slug}.txt").write_text(text, encoding="utf-8")
            page.set_content(terminal_html(f"readiness {argv[0]}", text))
            page.locator(".frame").screenshot(path=str(MEDIA / f"{slug}.png"))
            print(f"  {slug:<10} exit {code}  -> docs/media/{slug}.png")
            if code not in (0, 1):
                fail(f"`readiness {' '.join(argv)}` failed:\n{out}")

        # 2. dashboards of the committed example ledgers ------------------
        dash = browser.new_page(viewport={"width": 1180, "height": 900}, device_scale_factor=1.5)
        for name in EXAMPLES:
            target = scratch / f"dashboard-{name}.html"
            code, out = run_cli(["dashboard", "-c", name, "-o", str(target)], os.environ.copy())
            if code:
                fail(out)
            dash.goto(target.as_uri())
            dash.locator("#exp-0002").screenshot(path=str(MEDIA / f"reliability-{name}.png"))
            print(f"  dashboard  {name:<22} -> docs/media/reliability-{name}.png")
        code, out = run_cli(["dashboard", "--all"], os.environ.copy())
        if code:
            fail(out)
        dash.goto((ROOT / "experiments" / "index.html").as_uri())
        dash.screenshot(path=str(MEDIA / "dashboard-index.png"), full_page=True)

        # 3. the report ----------------------------------------------------
        report = browser.new_page(viewport={"width": 1280, "height": 860}, device_scale_factor=1.5)
        report.goto((ROOT / "report" / "index.html").as_uri())
        report.wait_for_timeout(1500)  # web fonts, if the network is there
        report.screenshot(path=str(MEDIA / "report-top.png"))
        report.locator('section[data-screen-label="02 The preparedness gap"]').screenshot(
            path=str(MEDIA / "report-section-2.png")
        )
        print("  report     -> docs/media/report-top.png, report-section-2.png")

        # 4. the loop, replayed as an animated GIF -------------------------
        lines = transcripts["loop"].rstrip("\n").split("\n")
        rec = browser.new_page(viewport={"width": 1004, "height": 640}, device_scale_factor=1)
        frames: list[Image.Image] = []
        step = max(1, len(lines) // 36)
        cut_points = list(range(1, len(lines) + 1, step))
        if cut_points[-1] != len(lines):
            cut_points.append(len(lines))
        for n in cut_points:
            rec.set_content(terminal_html("readiness loop", "\n".join(lines[:n]) + "\n"))
            rec.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            png = rec.screenshot()
            frames.append(Image.open(io.BytesIO(png)).convert("RGB").quantize(colors=64))
        durations = [140] * (len(frames) - 1) + [2600]
        gif = MEDIA / "loop.gif"
        frames[0].save(gif, save_all=True, append_images=frames[1:], duration=durations,
                       loop=0, optimize=True)
        print(f"  recording  -> docs/media/loop.gif ({gif.stat().st_size / 1e6:.2f} MB, "
              f"{len(frames)} frames)")

        # 5. scrolling through a dashboard, as a second GIF ---------------
        dash2 = browser.new_page(viewport={"width": 1180, "height": 760}, device_scale_factor=1)
        dash2.goto((scratch / f"dashboard-{DEMO_CONTRACT}.html").as_uri())
        height = dash2.evaluate("document.body.scrollHeight")
        frames = []
        y = 0
        while True:
            dash2.evaluate(f"window.scrollTo(0, {y})")
            dash2.wait_for_timeout(30)
            frames.append(Image.open(io.BytesIO(dash2.screenshot())).convert("RGB")
                          .quantize(colors=96))
            if y + 760 >= height:
                break
            y += 240
        durations = [1400] + [220] * (len(frames) - 2) + [2400]
        gif = MEDIA / "dashboard.gif"
        frames[0].save(gif, save_all=True, append_images=frames[1:], duration=durations,
                       loop=0, optimize=True)
        print(f"  recording  -> docs/media/dashboard.gif ({gif.stat().st_size / 1e6:.2f} MB, "
              f"{len(frames)} frames)")
        browser.close()

    manifest = {
        "demo_contract": DEMO_CONTRACT,
        "registered_in_demo": NEW_CONTRACT[0],
        "commands": {slug: argv for slug, argv in COMMANDS},
        "examples": list(EXAMPLES),
    }
    (MEDIA / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.rmtree(scratch, ignore_errors=True)
    print(f"done: {sum(1 for _ in MEDIA.glob('*.png'))} screenshots, 2 recordings, "
          f"{len(COMMANDS)} transcripts")


if __name__ == "__main__":
    main()
