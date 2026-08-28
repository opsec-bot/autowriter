"""The one file this package writes: the cached OAuth token."""

from __future__ import annotations

import os
import stat

import pytest

from autowriter.gdocs import auth
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


# ---------------------------------------------------------------------------
# A cached sign-in is a credential on its own
# ---------------------------------------------------------------------------


class _Credentials:
    """The shape google.oauth2.credentials.Credentials presents."""

    def __init__(self, valid=True, expired=False, refresh_token="refresh", fail=None):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.refreshed = False
        self._fail = fail

    def refresh(self, request):
        if self._fail is not None:
            raise self._fail
        self.refreshed = True
        self.valid = True
        self.expired = False

    def to_json(self):
        return '{"refreshed": true}'


def _fake_google(monkeypatch, credentials):
    """Stand in for the optional Google libraries, which CI does not install."""

    class _Module:
        class Credentials:
            @staticmethod
            def from_authorized_user_file(path, scopes):
                return credentials

        @staticmethod
        def Request():
            return object()

    monkeypatch.setattr(auth, "_require", lambda module: _Module)
    return _Module


def test_no_token_file_is_not_a_credential(tmp_path):
    assert auth._cached_token(str(tmp_path / "absent.json"), list(auth.SCOPES)) is None


def test_a_valid_cached_token_is_used_as_it_stands(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    credentials = _Credentials(valid=True)
    _fake_google(monkeypatch, credentials)

    assert auth._cached_token(str(token), list(auth.SCOPES)) is credentials
    assert not credentials.refreshed


def test_an_expired_token_refreshes_itself_and_is_written_back(tmp_path, monkeypatch):
    # The file carries its own client id and secret, so signing in once is
    # enough: the client secrets file is not needed again afterwards.
    token = tmp_path / "token.json"
    token.write_text('{"stale": true}', encoding="utf-8")
    credentials = _Credentials(valid=False, expired=True)
    _fake_google(monkeypatch, credentials)

    assert auth._cached_token(str(token), list(auth.SCOPES)) is credentials
    assert credentials.refreshed
    assert token.read_text(encoding="utf-8") == '{"refreshed": true}'


def test_a_token_that_cannot_be_refreshed_says_how_to_sign_in_again(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    _fake_google(monkeypatch, _Credentials(valid=False, expired=True, fail=ValueError("invalid_grant")))

    with pytest.raises(auth.AuthError) as error:
        auth._cached_token(str(token), list(auth.SCOPES))
    assert "setup --login --force" in str(error.value)


def test_a_token_with_nothing_to_refresh_with_is_not_a_credential(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    _fake_google(monkeypatch, _Credentials(valid=False, expired=True, refresh_token=None))

    assert auth._cached_token(str(token), list(auth.SCOPES)) is None


def test_a_cached_sign_in_is_found_without_naming_the_client_secrets(tmp_path, monkeypatch):
    """The regression: a perfectly good token was ignored unless the caller
    also passed --client-secrets, so `setup` reported no credentials at all."""
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    credentials = _Credentials(valid=True)
    _fake_google(monkeypatch, credentials)
    monkeypatch.delenv("AUTOWRITER_SERVICE_ACCOUNT", raising=False)
    monkeypatch.delenv("AUTOWRITER_CLIENT_SECRETS", raising=False)

    found = auth.load_credentials(token_file=str(token))

    assert found is credentials


def test_without_a_cached_token_it_still_falls_through_to_adc(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOWRITER_SERVICE_ACCOUNT", raising=False)
    monkeypatch.delenv("AUTOWRITER_CLIENT_SECRETS", raising=False)
    tried = []

    class _GoogleAuth:
        @staticmethod
        def default(scopes=None):
            tried.append(scopes)
            return "adc-credentials", "project"

    monkeypatch.setattr(auth, "_require", lambda module: _GoogleAuth)

    assert auth.load_credentials(token_file=str(tmp_path / "absent.json")) == "adc-credentials"
    assert tried
