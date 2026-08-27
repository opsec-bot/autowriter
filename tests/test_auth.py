"""The one file this package writes: the cached OAuth token."""

from __future__ import annotations

import os
import stat

from autowriter.gdocs.auth import _write_token


def test_a_bare_filename_has_no_directory_to_create(tmp_path, monkeypatch):
    # os.path.dirname("token.json") is "", and os.makedirs("") raises -- which
    # used to crash --token-file token.json *after* a successful sign-in.
    monkeypatch.chdir(tmp_path)
    _write_token("token.json", '{"refresh_token": "x"}')
    assert (tmp_path / "token.json").read_text(encoding="utf-8") == '{"refresh_token": "x"}'


def test_missing_directories_are_created(tmp_path):
    path = tmp_path / "nested" / "dir" / "token.json"
    _write_token(str(path), "{}")
    assert path.exists()


def test_the_token_is_never_readable_by_anyone_else(tmp_path):
    path = tmp_path / "token.json"
    _write_token(str(path), "{}")
    mode = stat.S_IMODE(os.stat(str(path)).st_mode)
    if os.name == "nt":  # POSIX permission bits are not enforced on Windows
        return
    assert mode == 0o600


def test_rewriting_an_existing_token_replaces_it(tmp_path):
    path = tmp_path / "token.json"
    _write_token(str(path), '{"first": true}')
    _write_token(str(path), '{"second": true}')
    assert path.read_text(encoding="utf-8") == '{"second": true}'
