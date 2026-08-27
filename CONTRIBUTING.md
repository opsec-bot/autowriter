# Contributing

## Setup

```bash
git clone https://github.com/opsec-bot/autowriter
cd autowriter
python -m venv .venv && . .venv/bin/activate    # .venv\Scripts\activate on Windows
pip install -e ".[google,dev]"
```

## Before opening a pull request

```bash
python -m pytest                 # 117 tests, no network, no credentials
python -m pyflakes autowriter tests
python scripts/validate_skill.py # only if you touched skills/ or .claude-plugin/
```

CI runs the same three on Python 3.8 through 3.13, on Linux, Windows and macOS.

## The rule that matters

`check` and `copy` must stay in agreement. The simulator in
`autowriter/gdocs/simulator.py` exists so that an offline run fails exactly
where a live one would; a permissive simulator is worse than no simulator,
because it turns a loud failure into a silent wrong answer.

So a fix for a live-API problem comes in two halves:

1. the builder stops emitting the bad request, and
2. the simulator learns to reject or model what the API actually does,

with a test for each. [docs/live-api-fixes.md](docs/live-api-fixes.md) records
the seven divergences found so far, in that shape.

## Reporting a bad copy

Open an issue with the fidelity report, the exact error, and — if you can share
it — a minimal `.docx` that reproduces it. `autowriter plan file.docx` prints
the requests, which is usually where the answer is.

## The skill

`skills/autowriter/SKILL.md` is what an agent reads. Keep it under 500 lines
and push detail into `skills/autowriter/reference/`; keep links one level deep
and use forward slashes. Bump the version in both `.claude-plugin/plugin.json`
and `.claude-plugin/marketplace.json` when the skill changes.
