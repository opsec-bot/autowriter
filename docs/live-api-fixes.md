# Live-API fixes

Every one of these was invisible to `check`: the simulator was more permissive
than the Docs API, so the offline run passed while the live copy failed. Each
fix therefore comes in two halves — the builder stops emitting the bad request,
and the simulator learns to reject or model what the API actually does, so
`check` stays worth running.

| # | Symptom against the live API | Cause | Fix |
|---|---|---|---|
| 1 | `400 Invalid requests[N].updateTextStyle: Links must include at least one type` | `text_style_payload` emitted `link: {}` to clear a link | Omit `link` from the payload, keep it in the `fields` mask (`requests.py`); simulator now rejects an empty link (`_check_text_style`) |
| 2 | `RuntimeError: table inserted at N could not be found` | `insertTable` puts a newline before the table, so it begins at `index + 1`; the simulator placed it at `index` | Builder addresses the table at `insert_at + 1`; simulator inserts after the paragraph and keeps one after the table |
| 3 | `400 Invalid requests[N].updateTableRowStyle: Unallowed field: tableHeader` | `tableHeader` is readable but not writable | Dropped from the request; a repeating header row is reported as unsupported |
| 4 | ~6,800 spurious differences, `fontSize expected 11.0, found None` | The API omits any property matching the named style's default | `verify` resolves replies against `namedStyles` before comparing |
| 5 | `indentFirstLine expected 0.0, found None` | Same, for paragraph styles; also "unset" vs "zero" | Paragraph styles resolved against `namedStyles`; indent/space family zero-defaulted on both sides |
| 6 | `fontFamily expected 'Calibri', found 'Arial'` on image characters | Images, page breaks and footnote references never got an `updateTextStyle`, despite the IR carrying one | `_style_placeholder` applies the run's style to all three |

| 7 | `KeyError: 'startIndex'` copying a document with a header | The API omits `startIndex` when it is zero -- exactly where a header, footer or footnote segment begins | Read it as `.get("startIndex", 0)`; simulator now omits a zero `startIndex` too |

Plus: the 429 backoff (1→2→4→8s) could never outwait a **per-minute** write
quota, so a rate-limited copy always exhausted its attempts. 429 now backs off
20→40→60s, with `tests/test_client.py` covering the retry path, which had none.

## Verified

`python -m pytest` — 105 passed. Ten documents copied to real Google Docs, all
reporting "every paragraph, character and property matches the source".
