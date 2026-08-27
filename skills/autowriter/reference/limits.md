# What cannot be copied, and why

Every limit below is a limit of the **Google Docs API**, not of the tool: there
is no request that would express the feature. Each one is named in the fidelity
report when the source document contains it, so a report that lists nothing has
nothing to hide.

Use this file to answer "why didn't X come across?" — quote the reason, don't
improvise a workaround.

## Contents

- Not reproducible at all
- Copied approximately
- Reading the fidelity report
- When a difference is a bug

## Not reproducible at all

| Feature | Why |
|---|---|
| Comments | No API request creates them |
| Bookmarks and internal links | No API request creates them |
| First-page and even-page headers/footers | `createHeader` only makes the default one |
| Per-section page size | Page size is document-wide in Google Docs; a landscape section cannot follow a portrait one, so the first section wins |
| Text boxes, shapes, WordArt, SmartArt, charts, equations | No API surface; skipped rather than approximated badly |
| A live table of contents | Docs can display one but nothing can create one through the API; a Word TOC arrives as the static text it was |
| Numbering that restarts at a given value | Cannot be expressed |
| Image alt text | No request sets it |
| A repeating table header row | `tableHeader` is readable but not writable |

## Copied approximately

| Feature | What happens |
|---|---|
| Field codes (`PAGE`, `DATE`, cross-references) | Copied as the cached result text Word last computed — they are plain text by the time they reach the API |
| Floating images | Placed inline; Docs has no equivalent anchor |
| Exact line spacing | Approximated by a multiple |
| Character tracking, raised/lowered text, double strikethrough, outline, shadow | Approximated or dropped |
| Bullet glyphs | Docs offers a fixed set of three-level glyph presets, so an unusual Word bullet gets the nearest one |
| Tracked changes | Insertions copied as final text, deletions left out — what Word shows with markup off |

## Reading the fidelity report

Three sections, in order of how much they should worry the reader:

```
Copied: 214 paragraphs, 6 tables, 3 images, 41,882 characters (487 requests in 23 batches)
Verification: every paragraph, character and property matches the source.

Copied approximately
--------------------
  - a floating image was placed inline (Docs has no equivalent anchor)

Not reproducible in Google Docs
-------------------------------
  - comments are not carried over
```

- **Verification** is the line that matters. After `copy` it is computed by
  reading the finished Google Doc back through the API and comparing it,
  property by property, against the source. After `check` it is computed
  against the simulator.
- **Copied approximately** and **Not reproducible** are the two tables above,
  filtered to what this document actually contains. They appear only when
  relevant.
- `--json` gives the same report as a structure, for scripting.

## When a difference is a bug

`check` runs against a simulator that enforces the same index rules and range
validation as the API. If the two disagree — `check` clean but `copy` fails, or
verification reporting a mismatch that is not on the lists above — that is a
defect in the tool. Capture the failing document (or a minimal version of it)
and the exact message, and open an issue at
<https://github.com/opsec-bot/autowriter/issues>. `docs/live-api-fixes.md`
records the seven such divergences found so far and how each was closed on both
sides.
