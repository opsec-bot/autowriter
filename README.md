# autowriter

Recreate a `.docx` inside a Google Doc **by hand** — one editing operation at a
time, the way a person would if they were retyping and reformatting the
document — instead of handing the file to Drive's importer and hoping.

```
$ autowriter check quarterly-report.docx
Source: quarterly-report.docx
Copied: 214 paragraphs, 6 tables, 3 images, 41,882 characters (487 requests in 23 batches)
Verification: every paragraph, character and property matches the source.

Copied approximately
--------------------
  - a floating image was placed inline (Docs has no equivalent anchor)

Not reproducible in Google Docs
-------------------------------
  - comments are not carried over
```

## Why not just upload the file?

Drive's `.docx` import is a black box: it decides what your document becomes,
it changes between one month and the next, and when it gets something wrong
there is no lever to pull. This does the opposite. Every character and every
property is placed by an explicit `documents.batchUpdate` request that you can
read, diff, and re-run:

```
$ autowriter plan report.docx | head -20
```

That makes the result predictable, reviewable, and — because the copy is read
back and compared against the source — checkable.

## Use it from Claude

autowriter ships as an [Agent Skill](https://code.claude.com/docs/en/skills), so
Claude can drive the whole thing for you — "put this Word document into Google
Docs" is the entire interface. Pick whichever install you already have a tool
for:

```bash
# Claude Code, as a plugin (also gets updates via /plugin marketplace update)
/plugin marketplace add opsec-bot/autowriter
/plugin install autowriter@autowriter

# any agent, via the skills CLI -- Claude Code, Cursor, opencode, ...
npx skills add opsec-bot/autowriter          # this project
npx skills add opsec-bot/autowriter -g       # every project

# or by hand: it is just a folder
git clone https://github.com/opsec-bot/autowriter
cp -r autowriter/skills/autowriter ~/.claude/skills/docx-to-google-docs
```

Then install the tool the skill drives, once:

```bash
pip install "autowriter[google] @ git+https://github.com/opsec-bot/autowriter"
```

That is the whole setup for `check`. For a real Google Doc, Claude runs
`autowriter setup`, reads back exactly what is missing, and works through it
with you one step at a time — handing you the sign-in command to run yourself
when it gets to that, because a browser prompt is not something an agent can
answer.
[skills/autowriter/reference/google-setup.md](skills/autowriter/reference/google-setup.md)
is the same ground click by click, if you would rather read it.

Ask for it in any of these shapes:

> convert quarterly-report.docx to a Google Doc
> Drive's import destroyed my formatting — can you do it properly?
> check whether this .docx will survive the trip before you copy it

## Install

```
pip install -e .              # the reader, the dry run, the checker
pip install -e '.[google]'    # plus the Google API clients, for writing
pip install -e '.[images]'    # plus Pillow, to convert TIFF/BMP/EMF images
```

Parsing, planning and checking need nothing but the standard library.

## Use

```
autowriter setup                   # what is missing before `copy` can work
autowriter check   report.docx     # copy into an in-memory Google Docs model, report fidelity
autowriter copy    report.docx     # copy into a real Google Doc
autowriter plan    report.docx     # print the batchUpdate requests as JSON
autowriter inspect report.docx     # print the parsed document structure
```

`check` runs the entire copy — the same requests, against a model that enforces
the same index rules the API does — and verifies the result, without a network
connection or a Google account. Run it first.

Useful flags: `--json` for machine-readable output, `--title` to name the new
document, `--document-id` to write into an existing (preferably empty) one,
`--no-verify` to skip reading the copy back, `--literal-caps` to keep the
stored casing of "All caps" text, `--keep-hidden` to include hidden text.

### Credentials

Start here:

```
$ autowriter setup
Google setup for `autowriter copy`

  [ok]      libraries    google-api-python-client, google-auth
  [missing] credentials  no service account key, OAuth token or gcloud login found

Next: sign in with gcloud
    gcloud auth application-default login --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/documents,https://www.googleapis.com/auth/drive.file
```

It checks the client libraries, the credential, its scopes, and whether both
APIs are actually enabled in that project — each of which otherwise surfaces
half way through a copy as a raw API error. It exits 0 when a copy will work,
and never opens a browser: `autowriter setup --login` is the separate,
deliberate step that does, and `--json` makes the whole diagnosis scriptable.

`copy` needs access to the Docs and Drive APIs. Enable both in a Google Cloud
project, then use whichever suits you:

```
# service account (headless; share the target Drive folder with it)
autowriter copy report.docx --service-account key.json

# installed-app OAuth (opens a browser once, then caches the token)
autowriter copy report.docx --client-secrets client_secret.json

# application default credentials
gcloud auth application-default login && autowriter copy report.docx
```

`AUTOWRITER_SERVICE_ACCOUNT` and `AUTOWRITER_CLIENT_SECRETS` work as
environment variables too. The scopes requested are `documents` and
`drive.file` — the latter only ever grants access to files this tool created.

### As a library

```python
from autowriter import read_docx
from autowriter.gdocs.builder import Copier
from autowriter.gdocs.client import ApiTransport
from autowriter.gdocs.verify import verify

document = read_docx("report.docx")
transport = ApiTransport(docs_service, document_id)
result = Copier(transport, image_uris).copy(document)
report = verify(document, transport.get_document())
```

Swap `ApiTransport` for `autowriter.gdocs.simulator.SimulatedDocs` to run the
same copy offline.

## What gets copied

| | |
|---|---|
| **Text** | every run, with bold, italic, underline, strikethrough, small caps, super/subscript, font family, size, colour, highlight, and links |
| **Paragraphs** | headings and named styles, alignment, line spacing, space before/after, left/right/first-line/hanging indents, keep-with-next, keep-lines-together, widow control, shading, borders, page-break-before |
| **Lists** | bullets and numbering, nesting levels, per-level indents, separate lists kept separate |
| **Tables** | cell text and formatting, column widths, header rows, row heights, merged cells (both directions), cell shading, borders, padding, vertical alignment, nested tables |
| **Page setup** | page size, margins, header/footer margins, section breaks with per-section margins and columns |
| **Other** | inline and floating images, page breaks, line breaks, tabs, headers, footers, footnotes, hyperlinks |

Formatting is resolved before it is written. A .docx paragraph usually states
almost nothing about itself and inherits the rest from `docDefaults`, its style
chain, and its numbering definition; Google Docs has its own, different
defaults. So the whole chain is flattened first and then stated explicitly —
which is why a heading that Word renders in 16pt Calibri Light `#2F5496` looks
the same in the copy rather than picking up Google's idea of a heading.

## What no tool can copy, and why

These are limits of the Google Docs API, not of this tool. Every one of them is
reported in the fidelity report when your document contains it, so you always
know what you are looking at:

- **Comments, bookmarks and internal links** — the API has no request to create
  them.
- **First-page and even-page headers/footers** — `createHeader` can only make
  the default one.
- **Per-section page size** — page size is document-wide in Google Docs, so a
  landscape section cannot follow a portrait one. The first section wins.
- **Text boxes, shapes, WordArt, SmartArt, charts and equations** — no API
  surface; they are skipped rather than approximated badly.
- **A table of contents** — Docs can display one, but nothing can create one
  through the API; a Word TOC arrives as the static text it was.
- **Field codes** (`PAGE`, `DATE`, cross-references) — copied as the cached
  result text Word last computed, because they are text by the time they reach
  the API.
- **Exact line spacing**, character tracking, raised/lowered text, double
  strikethrough, outline and shadow effects — approximated or dropped.
- **Bullet glyphs** — Docs offers a fixed set of three-level glyph presets, so
  an unusual Word bullet gets the nearest one. Numbering that restarts at a
  given value cannot be expressed at all.
- **Image alt text** — no request sets it.
- **Tracked changes** — insertions are copied as final text, deletions are left
  out, matching what Word shows with markup off.

## How it works

Four stages: read the package, flatten the formatting, plan the edits, verify
the result.

```
.docx ──▶ docxread ──▶ ir.Document ──▶ gdocs.builder ──▶ batchUpdate requests
                                            │
                            simulator ◀─────┴─────▶ live Docs API
                                            │
                                        gdocs.verify ──▶ fidelity report
```

The hard part is index arithmetic. The Docs API addresses everything by
position, in UTF-16 code units, and every insertion shifts everything after it.
Three rules keep it honest, and they are why the code is shaped the way it is:

1. **Append forward only.** Content always goes in at the end of what has been
   written, so ranges recorded earlier never move.
2. **Order inside a batch is a contract.** Requests see the document as the
   preceding requests left it, so each batch is emitted as: inserts, named
   styles, paragraph styles, character styles, then bullets *in reverse
   document order* — `createParagraphBullets` deletes the leading tabs that
   encode nesting depth, which moves everything after it — then the re-indent
   pass, with indices already adjusted for those deleted tabs.
3. **Re-read when the footprint is unknowable.** How many indices a table
   occupies is Docs' business, so tables are inserted, the document is fetched,
   and cells are filled *back to front*: filling a later cell cannot disturb an
   earlier one's position.

`autowriter.gdocs.simulator` implements enough of the Docs document model to
apply those same requests in memory, with the same index rules and the same
range validation. It is what makes `check` possible and what the test suite
runs against — index arithmetic is not something you can verify by reading.

## Tests

```
python -m pytest
```

149 tests, no network, no credentials, no fixture binaries: the .docx files are
assembled from raw XML at test time (`tests/fixtures.py`), copied through the
simulator, and verified.

The packaged skill and the plugin manifests have their own check:

```
python scripts/validate_skill.py
```

## Layout

```
autowriter/
  ir.py             intermediate representation: the fully-resolved document
  units.py          twips, half-points, EMU, UTF-16 index units
  report.py         the fidelity report
  cli.py            setup / check / copy / plan / inspect
  docxread/         package, styles, numbering, properties, reader
  gdocs/            requests, builder, simulator, verify, client, auth, doctor
skills/autowriter/  the Agent Skill: SKILL.md plus its reference files
.claude-plugin/     plugin and marketplace manifests, for /plugin install
scripts/            validate_skill.py
docs/               live-api-fixes.md -- the seven bugs the simulator hid
tests/              149 tests, fixtures assembled from raw XML
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under the
[MIT License](LICENSE).
