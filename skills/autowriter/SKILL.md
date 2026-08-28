---
name: docx-to-google-docs
description: Recreates a .docx file inside a Google Doc one editing operation at a time, preserving fonts, styles, lists, tables, images, headers and page setup, and reports exactly what could not be reproduced. Use when the user wants to convert, import, copy or migrate a Word document (.docx) into Google Docs, when Drive's own import mangled a document, or when they mention the autowriter tool.
---

# .docx to Google Docs

Copies a Word document into a Google Doc with explicit `documents.batchUpdate`
requests instead of Drive's importer, then reads the copy back and compares it
against the source. Every difference is reported rather than silently accepted.

The command is `autowriter`. It has four subcommands:

| Command | Network | Credentials | What it does |
|---|---|---|---|
| `autowriter setup` | yes | no | Says what is missing before `copy` can work, and the command that fixes it |
| `autowriter check FILE.docx` | no | no | Runs the whole copy against an in-memory Docs model and reports fidelity |
| `autowriter copy FILE.docx` | yes | yes | Copies into a real Google Doc |
| `autowriter plan FILE.docx` | no | no | Prints the batchUpdate requests as JSON |
| `autowriter inspect FILE.docx` | no | no | Prints the parsed document structure |

## Workflow

Copy this checklist and work through it:

```
- [ ] Step 1: Confirm autowriter is installed
- [ ] Step 2: Run `check` and read the fidelity report to the user
- [ ] Step 3: Run `autowriter setup` and clear whatever it names (copy only)
- [ ] Step 4: Run `copy` and give the user the document URL
```

**Step 1 — confirm the tool is installed.**

```bash
autowriter --help
```

If that fails, install it (see "Installing autowriter" below).

**Step 2 — always run `check` first.**

```bash
autowriter check report.docx
```

This performs the identical copy offline, so it costs nothing and needs no
account. Report its three sections to the user verbatim in substance: what was
copied, what was copied *approximately*, and what is *not reproducible in
Google Docs*. Do not describe a copy as lossless when the report lists
approximations — read [reference/limits.md](reference/limits.md) to explain any
line the user asks about.

If `check` reports differences other than the known-unsupported list, stop and
show the output; that is a bug worth reporting, not something to work around.

**Step 3 — setup, only for `copy`.**

Do not guess at what is configured, and do not read the walkthrough at people
who do not need it. Ask the tool:

```bash
autowriter setup --json
```

It exits 0 when a copy will work, 1 when something is missing, 2 on an error.
The JSON carries `ready`, an ordered `checks` array, and `nextStep` — the one
thing to fix now, with the exact command that fixes it. Do them in order: a
credential cannot be scope-checked before it exists, and an API cannot be
probed without one.

Run the `nextStep.command` yourself **unless it signs someone in**. Those two
open a browser and then block until a human finishes; started from here they
hold the tool call open until it times out, and the sign-in is lost. Hand them
to the user instead, prefixed with `!` so they run in their own terminal:

```
! autowriter setup --login --client-secrets client_secret.json
! gcloud auth application-default login --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/documents,https://www.googleapis.com/auth/drive.file
```

Say plainly that a browser window will open and that you will wait. Re-run
`autowriter setup` afterwards — it is the source of truth, not their report of
what happened.

What no command can do for them: creating the OAuth client and downloading
`client_secret.json` is Cloud Console clicking, and there is no API for it.
That is the one time to walk through
[reference/google-setup.md](reference/google-setup.md), and only the part they
are stuck on. If `gcloud` is installed — `setup` reports this as `gcloud` in
the JSON — the ADC route avoids the console entirely and is the better
suggestion.

Never invent a project id, a key path, or a client id.

**Step 4 — copy.**

```bash
autowriter copy report.docx --service-account key.json     # headless
autowriter copy report.docx --client-secrets secret.json   # opens a browser once
autowriter copy report.docx                                # uses ADC
```

The command prints the new document's URL and the same fidelity report, this
time computed by reading the finished Google Doc back. Give the user the URL.

Install with the Google client libraries or `copy` will fail on import:
`pip install "autowriter[google]"`. `autowriter setup` reports that too.

## Useful flags

- `--json` — machine-readable report; use it when post-processing, not when
  showing the user.
- `--title "Name"` — title of the new document.
- `--document-id ID` — write into an existing document. It should be empty;
  content is appended after whatever is already there.
- `--no-verify` — skip reading the copy back. Only for very large documents
  where the read costs quota; the fidelity claim is then unverified, so say so.
- `--literal-caps` — keep the stored casing of "All caps" text instead of
  upper-casing it.
- `--keep-hidden` — include text marked hidden in Word.
- `--no-headers`, `--no-footnotes`, `--no-images` — leave that class of
  content out of the copy.
- `--token-file PATH` — where to cache the OAuth token
  (default `~/.autowriter/token.json`).

## Installing autowriter

The tool is a pure-standard-library Python package for everything except
`copy`.

```bash
pip install "autowriter[google] @ git+https://github.com/opsec-bot/autowriter"
```

From a clone, for development:

```bash
pip install -e ".[google,dev]"
```

Add the `images` extra (`Pillow`) only if the document contains TIFF, BMP or
EMF images; the tool says so when it needs it.

## Rules

- Run `check` before `copy`, every time. It is free and catches real problems.
- Never start a sign-in from a tool call. `autowriter setup --login` and
  `gcloud auth application-default login` block on a human at a browser; give
  them to the user with `!` and wait.
- Report limitations rather than papering over them. A Word feature Google Docs
  cannot express (comments, text boxes, per-section page size, a real table of
  contents) is listed in the report and explained in
  [reference/limits.md](reference/limits.md).
- Do not re-implement the conversion. If something is wrong, the fix belongs in
  the tool, not in a one-off script that patches the output.
- The requested scopes are `documents` and `drive.file`; `drive.file` only ever
  grants access to files this tool created. Do not widen them.
