"""Check that the packaged skill and the plugin manifests are well formed.

Run by CI, and worth running by hand after editing anything under skills/ or
.claude-plugin/: a malformed manifest fails at install time, on someone else's
machine, with a message that does not say which field was wrong.
"""

from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The frontmatter rules the Skills loader enforces.
NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")
RESERVED = ("anthropic", "claude")
MAX_DESCRIPTION = 1024
MAX_BODY_LINES = 500

problems = []


def fail(message: str) -> None:
    problems.append(message)


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def check_skill(directory: str) -> None:
    relative = os.path.relpath(directory, ROOT).replace(os.sep, "/")
    path = os.path.join(directory, "SKILL.md")
    if not os.path.exists(path):
        fail("%s: no SKILL.md" % relative)
        return

    text = read(path)
    if not text.startswith("---\n"):
        fail("%s: SKILL.md does not open with YAML frontmatter" % relative)
        return

    end = text.find("\n---\n", 4)
    if end == -1:
        fail("%s: frontmatter is not closed" % relative)
        return

    frontmatter, body = text[4:end], text[end + 5 :]
    fields = {}
    for line in frontmatter.splitlines():
        if line[:1].isspace() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()

    name = fields.get("name", "")
    description = fields.get("description", "")

    if not NAME_PATTERN.match(name):
        fail("%s: name %r must be 1-64 lowercase letters, numbers or hyphens" % (relative, name))
    if any(word in name for word in RESERVED):
        fail("%s: name %r contains a reserved word" % (relative, name))
    if not description:
        fail("%s: description is empty" % relative)
    if len(description) > MAX_DESCRIPTION:
        fail("%s: description is %d characters, over the %d limit" % (relative, len(description), MAX_DESCRIPTION))
    for field, value in (("name", name), ("description", description)):
        if "<" in value and ">" in value:
            fail("%s: %s must not contain XML tags" % (relative, field))

    lines = len(body.splitlines())
    if lines > MAX_BODY_LINES:
        fail("%s: SKILL.md body is %d lines, over the %d line budget" % (relative, lines, MAX_BODY_LINES))

    for target in re.findall(r"\]\((?!https?:)([^)#]+)", body):
        if not os.path.exists(os.path.join(directory, target)):
            fail("%s: SKILL.md links to %s, which does not exist" % (relative, target))
        if "\\" in target:
            fail("%s: link %s uses a backslash; use forward slashes" % (relative, target))

    print("ok   %s (%s, %d line body)" % (relative, name, lines))


def check_json(path: str, required: "tuple[str, ...]") -> dict:
    relative = os.path.relpath(path, ROOT).replace(os.sep, "/")
    try:
        data = json.loads(read(path))
    except (OSError, ValueError) as error:
        fail("%s: %s" % (relative, error))
        return {}
    for key in required:
        if key not in data:
            fail("%s: missing required field %r" % (relative, key))
    print("ok   %s" % relative)
    return data


def main() -> int:
    skills_root = os.path.join(ROOT, "skills")
    if not os.path.isdir(skills_root):
        fail("skills/ is missing")
    else:
        found = False
        for entry in sorted(os.listdir(skills_root)):
            directory = os.path.join(skills_root, entry)
            if os.path.isdir(directory):
                found = True
                check_skill(directory)
        if not found:
            fail("skills/ contains no skill directories")

    check_json(os.path.join(ROOT, ".claude-plugin", "plugin.json"), ("name", "version"))
    marketplace = check_json(
        os.path.join(ROOT, ".claude-plugin", "marketplace.json"), ("name", "owner", "plugins")
    )
    for plugin in marketplace.get("plugins", []):
        source = plugin.get("source")
        if isinstance(source, str) and not os.path.exists(os.path.join(ROOT, source)):
            fail("marketplace.json: plugin source %s does not exist" % source)

    for problem in problems:
        print("FAIL %s" % problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
