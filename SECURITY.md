# Security

## Reporting a vulnerability

Open a [private security advisory](https://github.com/opsec-bot/autowriter/security/advisories/new).
Please do not open a public issue for anything that affects other people's
documents or credentials.

## What this tool touches

**Credentials.** The only file autowriter writes outside the document you point
it at is the cached OAuth token, `~/.autowriter/token.json`, created with mode
`0600`. Service account keys and client secrets are read, never copied. The
scopes requested are `documents` and `drive.file`; `drive.file` grants access
only to files this tool itself created, never to the rest of your Drive.

**Images are briefly public.** `insertInlineImage` takes a URL, not bytes, and
Google's fetcher must be able to reach it. So each image is uploaded to Drive,
shared with "anyone with the link", inserted — Docs copies it into the document
at that moment — and then deleted. The window is seconds, the link is an
unguessable file id, and the upload is removed even if the copy fails part way
through. `--keep-image-uploads` disables that cleanup: the copies stay
world-readable until you delete them, so use it only for debugging.

**Untrusted .docx files.** Parts are read straight out of the zip and never
extracted to disk, so a crafted path in the archive cannot escape anywhere.
Relationship targets marked external are not fetched: no network request is
made on the strength of anything inside a document. XML is parsed with
`xml.etree.ElementTree`, which does not resolve external entities.

Two denial-of-service shapes are *not* defended against, because both cost the
attacker nothing and cost you only the process: a zip bomb (a small archive
whose parts decompress to many gigabytes) and an entity-expansion bomb, which
`ElementTree` will happily expand. Treat a `.docx` from a stranger the way you
would treat any other untrusted file, and run it somewhere you do not mind
losing.

## What is out of scope

The fidelity report describing a Google Docs limitation is not a vulnerability;
see [skills/autowriter/reference/limits.md](skills/autowriter/reference/limits.md).
