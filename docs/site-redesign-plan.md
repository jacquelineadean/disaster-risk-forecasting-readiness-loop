# Site redesign plan: the research-microsite look

Status: implemented on 2026-09-13, on the branch that carries this file. Two
details differ from the text below: the button modifiers kept their existing
names (`.btn.secondary`, `.btn.ghost`, `.btn.small`) rather than the `--`
variants of section 5, and smooth scrolling was dropped so that deep links land
deterministically under the sticky bars.

Scope: `site/` (five pages, `assets/site.css`, `assets/site.js`, `assets/ledgers.js`,
`assets/playground.js`), `tools/build_site.py`, `tests/test_site.py`. The sandbox
(`assets/sandbox-worker.js`, `assets/sandbox.py`), the Pages workflow and the Python
package are untouched.

## 1. Why and what

The site that shipped in #8 has a "paper briefing" look: cream paper, a serif display
face, hairline boxes, a two-column body with margin notes. The brief is to restyle it
after the calm, product-marketing style of Google Research's flood-forecasting
microsite while keeping every function: the generated-data pages, the in-browser
ledger verification, the Pyodide sandbox and its six demos, every deep link and hash
route, and the tests.

This is design inspiration only. Section 3 draws the line between borrowing a style
and copying a site.

## 2. What the reference does (design audit)

Observed on 2026-09-13 at 1440 px and 375 px; sizes are computed styles read from the
page, not guesses.

**Page anatomy**

- A 64 px white header: wordmark and product name on the left, three text links on
  the right. Fixed on desktop and given a shadow once scrolled; on phones a menu
  button, with the product name dropping to a second row.
- A full-bleed hero 720 px tall (650 on phones): an ambient, muted, looping video
  with a pause control. The title (80 px, weight 450) and three dark pill buttons are
  overlaid bottom-left. On phones the buttons stack full width.
- Everything else lives in one centred 920 px reading column on white. Blocks are
  separated by 80 px of whitespace; no rules, no boxes, no background bands.
- Section headings 42 px / weight 450 / line-height 1.04, sitting 10 px above a
  17.5 px / 1.45 grey body; paragraphs 15 px apart. Phones: 28 px headings.
- Media is column-wide (920 px). Inline videos have a 4 px radius; images inside
  cards have a 16 px radius and a 1 px hairline at 6 % alpha.
- Card rows sit on a 12-column grid with 64 px gutters: two-up cards are 428 px
  wide, three-up 264 px. A card is image, 10 px uppercase eyebrow (0.8 px
  letter-spacing), 20 px / 500 title, grey description and a plain text link. No
  border, no shadow, no background.
- Sub-pages: a 72 px h1 with no media; a sticky 50 px jump-link bar (white,
  hairline, light shadow, 16 px / 500 grey links) on the long page; a publications
  list of rows 24 px tall in padding, separated by hairlines, 17.5 px title and small
  grey meta.
- Footer: one 80 px row, wordmark plus five text links.

**Tokens**

- Colour: white ground; near-black ink for headings; dark grey body text; mid grey for
  navigation and meta; a 6 %-alpha hairline. Brand blue appears only in the wordmark
  and inside illustrations. No semantic colours anywhere on the page.
- Type: one variable sans at weights 400 / 450 / 500 with optical sizing. Nothing
  bold; italics only in small captions.
- Spacing scale 8 / 16 / 24 / 36 / 48 / 60 / 92 / 136; vertical page padding
  28 / 40 / 72 (phone / tablet / desktop).
- Radii: 16 px for media and cards, 999 px for buttons. Buttons: 12 × 24 px padding,
  17.5 px / 450, 0.2 s colour transition.
- Breakpoints 600 / 1024 / 1440; page gutters 28 / 40 / 72 px; content max 1296 px.

**Character**: quiet and editorial. Imagery carries the page; type is large, light
and unhurried; hierarchy comes from size and whitespace rather than rules, tints or
colour; there is only ever one column of reading. Motion is ambient and pausable.

## 3. The line: inspiration, not imitation

Take: the anatomy (bar, bleed hero, one reading column, whitespace rhythm); the type
posture (large light headings, grey body, no bold); the surfaces (16 px radii, pills,
hairlines instead of boxes); the media-led card; the sticky jump bar; the hairline
list; the minimal footer; the ambient, pausable hero.

Do not take: any text, image, video, icon or logo; the Google header lock-up or any
Google wordmark; Google Sans, Google Sans Flex or Product Sans (Google's brand
typefaces, which would read as imitation and are not licensed for third-party sites);
the Glue CSS framework, its class names (`glue-*`) or its markup; the exact colour
values; the product framing; the copy structure of the sections.

Everything visual on our site stays ours: the wordmark, the copy, the SVG diagrams,
the reliability diagrams, the terminal frames, and a hero animation generated from
our own pinned data.

## 4. Design system for the new site

Tokens, in `site/assets/site.css` under `:root`:

```
--bg:#fff; --ink:#15171b; --text:#3f434b; --muted:#6b7079;
--hair:rgba(21,23,27,.08); --surface:#f4f6f8; --surface-2:#e9edf2;
--accent:#2b5aa8; --accent-ink:#1f4482; --accent-wash:#e8effa;
--pass:#2f7d4f; --fail:#b03a2e; --reject:#8a1c9e; --warn:#9a6a10;   /* unchanged */
--term-*                                                            /* unchanged */
--sans:"DM Sans",system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
--mono:                                                             /* unchanged */
--s1:8px; --s2:16px; --s3:24px; --s4:36px; --s5:48px; --s6:64px; --s7:96px; --s8:136px;
--r-media:16px; --r-ctl:8px; --r-pill:999px;
--page:1296px; --col:920px; --col-wide:1120px;
```

Typeface: DM Sans (variable, optical size 9–40 and weight 100–1000, SIL Open Font
License, served from Google Fonts). One family for display and body at weights
400 / 450 / 500 only; one `<link>` replacing Instrument Serif and Public Sans in all
five heads. Alternatives if preferred: Figtree, Instrument Sans. Instrument Serif is
dropped: the reference is single-family, and keeping the serif keeps the old identity.

Type scale (desktop → phone):

| role | size / line | weight, colour |
|---|---|---|
| display (hero h1) | clamp(44px, 5.6vw, 76px) / 1.04 | 450, ink |
| h2 (section) | clamp(30px, 3.2vw, 42px) / 1.1 | 450, ink |
| h3 | 22 / 1.3 | 500, ink |
| card title | 20 / 1.3 | 500, ink |
| eyebrow | 11 / 1, uppercase, .08em | 600, muted |
| body | 17 / 1.55 | 400, text |
| small, meta, captions | 14 / 1.5 | 400, muted; captions italic |
| tables | 14; numbers tabular | |
| mono | 13 / 1.55 | |

Layout: `.page` (max 1296, gutters 24 / 40 / 72 at <600 / <1024 / ≥1024); `.col`
(max 920, centred, for reading); `.col-wide` (max 1120, for tables, diagrams and the
ledger cards); `.bleed` (full width). Grid: `.cards` is a CSS grid with a 24 px gap,
in `.cards--2`, `.cards--3`, `.cards--4`; three and four columns collapse to two
under 1024, everything to one under 600. Section rhythm: 96 px between blocks (64
under 1024, 40 under 600); 12 px between a heading and its body; 32 px around media.

Breakpoints: 600 / 1024 / 1440, replacing today's 520 / 760 / 900 / 1000.

## 5. Component mapping (today → new)

| today | new | notes |
|---|---|---|
| `.mast .bar` (2 px ink rule) | `header.top`: 64 px, sticky, hairline; a shadow class added by JS after 8 px of scroll | Left: wordmark "The Readiness Loop" at 20 px / 500 and a small "Phase 0" pill. Right: nav at 15 px / 500 muted, active in ink. Under 1024 a menu button toggles a stacked list (about 15 lines in site.js). |
| `header.hero` (meta row, h1, lede, two columns) | `.hero--bleed` on index, `.hero--plain` elsewhere | Bleed: 640 px tall (520 on phones), the animation as background, title, one line and three pills bottom-left, pause button bottom-right. Plain: h1 at 72 px and a one-paragraph lede in `.col`, 48 px of top padding. |
| `.eyebrow-row` with line, `h2`, `.cols` (`.main` plus `aside.note`) | `.section`: eyebrow (no line), h2, `.lede`, content in `.col` | Asides become `.aside`: a surface-tinted 16 px-radius note placed right after the paragraph it comments on. At ≥1440 `.aside--margin` floats it into the right margin, recovering today's margin-note feel without a second column of body text. |
| `.card` (white, 1 px border) | `.card`: surface background, 16 px radius, 24 px padding, no border; `.card--media` adds a figure on top (16 px radius, hairline); linkable cards darken slightly on hover | |
| `.grid-2` / `-3` / `-4` | `.cards.cards--N` | |
| `.callout .unit` (accent left rule) | `.unit`: a surface panel with the forecast unit at 30 px / 450; H, R and T in accent | |
| `.warnbox` | `.notice--caution`: warm tint, 16 px radius, no border | |
| `.diagram` (bordered white) | `.figure`: surface panel, 16 px radius, horizontal scroll if needed. SVGs restyled: white boxes with 8 px radius and hairline strokes, the harness box in accent wash, 13 px / 500 labels, muted arrows. The index pipeline is rebuilt as HTML flex boxes so it reflows vertically on phones. | |
| tables (bordered, white) | No outer border; eyebrow-style header row; hairline rows; `.table-scroll` wrapper | |
| `.tag.*` | `.pill.*`: 12 px / 600, 3 × 10 px padding, radius 999; the same five semantics | JS emits `.pill` |
| `.btn`, `.secondary`, `.ghost`, `.small` | `.btn` pill: `--primary` (ink background, white text), `--secondary` (hairline outline, ink text), `--text` (accent text); `--sm` | 0.2 s transition; disabled at 40 % opacity |
| `.term` | Structure unchanged; 16 px radius, softer shadow, subtler title bar | Colour classes unchanged |
| `.step` | Number at 40 px / 300 in accent, h3 at 24 / 500, run links as small secondary pills | |
| `.tabs` (ledgers) | `.jumpbar`: sticky under the header, 48 px, white, hairline; links 15 px / 500 muted, active in ink with a 2 px accent underline; scrolls horizontally on phones | Reused on design (12 sections), walkthrough (12 steps) and playground (6 demos plus the terminal) |
| `details.demo` | `.demo` accordion: surface background, 16 px radius, summary at 16 px / 500 with a numbered eyebrow and a chevron, 24 px inner padding | ids unchanged |
| `.field` (label to the left) | `.field` stacked: 13 px / 500 muted label above a 40 px control with 8 px radius, hairline border and accent focus ring; `.field--inline` for checkboxes; `.field--range` keeps the value readout | |
| `fieldset` / `legend` | `.fieldgroup` with an eyebrow heading | |
| `.chip` | Outlined pill in 12 px mono | |
| `.status` card and progress bar | `.statusbar`: one row (dot, text, a 2 px progress line), no card | |
| `iframe.dash` | 16 px radius and a hairline | |
| `footer.site` | One hairline; 14 px muted; credits left, built stamp and links right | |

## 6. The hero: our own ambient media

The reference leads with satellite imagery. We have no photography and will not
fabricate any; our imagery is the data. Proposal: **the panel tape**, a canvas
animation of the tornado-ok panel itself.

- One tile per county (77, laid out 11 × 7 in FIPS order) and one frame per quarter
  from 1996 to 2025 (120 frames) at 0.2 s a frame, a 24 s loop. A tile lights in
  accent when that county-quarter is a damaging-event unit and fades over the next
  few frames. A thin timeline underneath is tinted by split (train, validate, test)
  with the year at the playhead and the running base rate.
- Drawn in muted tones behind a white-to-transparent gradient on the left so the
  title stays legible. Pausable. `prefers-reduced-motion` and leaving the viewport
  (IntersectionObserver) stop it; the first frame is the static poster.
- Data: `tools/build_site.py` writes `generated/hero.json` with the contract name,
  the region list, the period list, the labels as a base64 bit string (9,240 bits),
  the split years and the base rate, from the same panel it already builds for
  `panels.json`. It is built only when the pinned data is present; the page falls
  back to a static SVG poster when the file is missing. About 60 lines in the build
  and 120 in a new `assets/hero.js`.
- Alternatives considered: a reliability diagram morphing between the four ledger
  cards, and the loop GIF in a device frame. The panel tape is the one that explains
  the forecast unit before a word is read.

## 7. Page by page

**index.html**

1. Top bar.
2. Bleed hero: h1 unchanged, a one-line sub, pills "Run the loop in your browser"
   (primary), "Walkthrough" and "System design" (secondary), the panel tape.
3. "The forecast unit": h2, the `.unit` panel, two paragraphs, the "Why so small"
   aside.
4. "What a run does": h2, the pipeline as reflowing boxes, the four stage cards
   two by two (four-up at ≥1024), the `make loop` line.
5. "Examples": three `.card--media` contract cards. The media is the contract's own
   reliability diagram, drawn by `RL.reliabilitySVG` from the seasonal card, or its
   panel-tape poster; eyebrow is the hazard; then the kv list, the mini verdict
   table and text links "Ledger" and "Run in the sandbox".
6. The four assertions as a numbered list.
7. "Harness before model": the quote at 24 px / 400, the verify transcript, the
   invariants aside.
8. "Working demos": six cards three-up with eyebrows "DEMO 01" to "DEMO 06".
9. The caution notice.
10. Footer.

**design.html**: plain hero (h1 and lede). Jump bar with twelve short labels
(Planes, Invariants, Contract, Data, Panel, Models, Harness, Canary, Ledger, Agents,
Commands, Roadmap). Each section is eyebrow "01", h2, lede and content in `.col`;
tables and the two SVG diagrams in `.col-wide` figures; code blocks on surface
panels; the ten margin notes become `.aside` (margin variant at ≥1440).

**walkthrough.html**: plain hero with the loop GIF in a 16 px frame under the lede;
the text pipeline `<pre>` becomes the same reflowing boxes as index. Jump bar "1" to
"12", with labels at ≥1024. Steps stacked in `.col`; transcripts unchanged; the
diagrams grid and the compare grid as `.cards`.

**ledgers.html**: plain hero. The contract tabs become the jump bar, with hash
routing unchanged (`#tornado-ok`, `#tornado-ok/exp-0002`). Header kv and describe
block in `.col`; the summary table; the chain card with the attack menu as a select
and pill buttons; experiment cards two-up in `.col-wide`, each a surface card with
the reliability diagram, verdict pill, checks and findings; the fingerprints table;
the compare section three-up.

**playground.html**: plain hero with a shorter lede and the `.statusbar` beneath.
Jump bar "01" to "06" and "Terminal". At ≥1024 two columns inside `.page` (7 and 5
of 12: demos left, terminal sticky right); below that, stacked, with the terminal
after the demos and the jump link to it. Demos as `.demo` accordions with stacked
fields; presets and chips as pills; the coverage list as a small table; the dashboard
iframe framed.

Not restyled: `generated/report/index.html` (the research briefing has its own
design) and the dashboard HTML that `readiness/dashboard.py` renders into the iframe.

## 8. Code changes by file

- `site/assets/site.css`: rewrite, about 550 lines: tokens, base, layout, header,
  jump bar, footer, hero, sections and asides, cards and figures, tables, pills,
  buttons, terminal (kept), diagrams, steps, forms, accordion, status, playground
  grid, responsive rules at 600 / 1024 / 1440, print.
- `site/index.html`, `design.html`, `walkthrough.html`, `ledgers.html`,
  `playground.html`: new head (font link), new header and footer, section markup as
  in section 7. Every id, `data-*` hook, hash and query parameter is kept.
- `site/assets/site.js`: class names in `RL.terminal`, `verdictTag`, `checksList`,
  `findingsList`, `summaryTable`, `runLink` and `sandboxLink`; new: the
  sticky-header shadow, the mobile menu toggle, the jump-bar active state
  (IntersectionObserver), and `RL.thumb(card)` for card media.
- `site/assets/ledgers.js`: `cardHTML`, `fingerprintsHTML`, tabs to jump bar;
  behaviour unchanged.
- `site/assets/playground.js`: the result renderers, `renderCoverage`, `setStatus`
  (status bar), demo-hash opening. The worker protocol is untouched.
- `site/assets/hero.js` (new) and `tools/build_site.py` (`hero.json`).
- `tests/test_site.py`: the `hero.json` shape and bit count; the five pages share one
  identical header, nav and footer block; no `glue-` class and no Google-branded font
  anywhere under `site/`; the existing link check keeps passing.
- `README.md`, "The website": one sentence on the look. No functional change.

## 9. Order of work

Seven commits on one branch cut from main (main now carries #8). Each leaves the
site rendering and the tests green.

1. Foundation: fonts, tokens, base type, header, jump bar, footer, buttons, pills,
   tables, cards, notices. Pages receive the new chrome and hero variants and still
   render with the old section markup.
2. `index.html`.
3. `design.html` and `walkthrough.html`.
4. `ledgers.html`, `playground.html` and the class changes in the three JS files.
5. Hero animation: build output, `hero.js`, fallback poster.
6. Responsive, keyboard and reduced-motion pass at 375 / 768 / 1024 / 1440; print.
7. Tests and README.

Rough size: CSS about 550 lines rewritten, HTML about 300 lines touched, JS about
200 lines, Python about 60 lines, tests about 50 lines.

## 10. Verification before the PR is marked ready

- `make site`, then the preview at 375 / 768 / 1024 / 1440: no horizontal scroll, no
  console errors, fonts loaded.
- Sandbox flows re-run in the browser: `loop -c tornado-ok` reproduces the committed
  numbers (BSS +0.1201, AUC 0.8175); registering `hail-ok` and the two refusals; the
  calibration rescoring; the "edit" tamper breaks the chain at index 1 and restore
  mends it; bit-for-bit 32 of 32; the dashboard renders.
- Deep links: `playground.html?cmd=loop%20-c%20tornado-ok`,
  `playground.html#demo-register`, `ledgers.html#tornado-ok/exp-0002`,
  `design.html#canary`, and every walkthrough `data-run` link.
- Keyboard: jump bar, accordions, forms, terminal input; focus visible; reduced
  motion stops the hero.
- `python3 -m unittest discover -s tests -t .` (274 today, plus the new ones).

## 11. Decisions to confirm (recommendation first)

1. Typeface: DM Sans. Alternatives: Figtree, Instrument Sans.
2. Drop Instrument Serif entirely (recommended) rather than keep it for the forecast
   unit and quotes.
3. Hero media: the panel tape, not the morphing diagram or the framed GIF.
4. Light only (`color-scheme: light`, like the reference). No dark mode.
5. Keep the section numbering ("01 · Planes") as eyebrows: it is ours and useful.

## 12. Prerequisites and findings

- #8 merged on 2026-09-13 at 19:02 UTC. Its `site` workflow's build job succeeded
  (pinned data pulled, site built), but the deploy job failed because GitHub Pages is
  not enabled on the repository (a 404 creating the deployment). Enable it under
  Settings, Pages, Source "GitHub Actions", then re-run the workflow. The redesign
  does not change the workflow.
- The redesign branch must be cut from main after #8.
