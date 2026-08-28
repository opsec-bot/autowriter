"""`autowriter setup`: what it notices, and what it refuses to do on its own."""

from __future__ import annotations

import json
import types

import pytest

from autowriter.cli import main
from autowriter.gdocs import auth, doctor


class _Response:
    def __init__(self, status):
        self.status = status


class _HttpError(Exception):
    def __init__(self, status, message=""):
        super().__init__(message or "HTTP %d" % status)
        self.resp = _Response(status)


def _credentials(scopes=None):
    return types.SimpleNamespace(scopes=scopes)


@pytest.fixture
def installed(monkeypatch):
    """Pretend the optional Google libraries are present; they are not in CI."""
    monkeypatch.setattr(doctor, "missing_libraries", lambda: [])


@pytest.fixture
def no_gcloud(monkeypatch):
    monkeypatch.setattr(doctor, "has_gcloud", lambda: False)


@pytest.fixture
def gcloud(monkeypatch):
    monkeypatch.setattr(doctor, "has_gcloud", lambda: True)


def _names(diagnosis):
    return [check.name for check in diagnosis.checks]


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------


def test_missing_libraries_stop_the_diagnosis_there(monkeypatch, no_gcloud):
    monkeypatch.setattr(doctor, "missing_libraries", lambda: ["google-auth"])
    diagnosis = doctor.diagnose()

    # Nothing below can be established without them, so nothing below is guessed at.
    assert _names(diagnosis) == ["libraries"]
    assert not diagnosis.ready
    assert "pip install" in diagnosis.next_step.command


def test_no_credentials_recommends_gcloud_when_it_is_there(installed, gcloud, monkeypatch):
    monkeypatch.setattr(doctor, "load_credentials", _raising(auth.AuthError("nothing")))
    diagnosis = doctor.diagnose()

    step = diagnosis.next_step
    assert step.name == "credentials"
    assert step.command == doctor.GCLOUD_LOGIN
    assert "--scopes=" in step.command


def test_no_credentials_falls_back_to_the_console_walkthrough(installed, no_gcloud, monkeypatch):
    monkeypatch.setattr(doctor, "load_credentials", _raising(auth.AuthError("nothing")))
    diagnosis = doctor.diagnose()

    assert doctor.SETUP_REFERENCE in diagnosis.next_step.command


def test_client_secrets_without_a_token_asks_for_a_sign_in(installed, gcloud, monkeypatch, tmp_path):
    secrets = tmp_path / "client_secret.json"
    secrets.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(doctor, "load_credentials", _raising(auth.AuthError("not signed in")))

    diagnosis = doctor.diagnose(client_secrets=str(secrets))
    step = diagnosis.next_step

    assert "not signed in yet" in step.detail
    assert step.command.startswith("autowriter setup --login")


def test_a_credential_file_that_is_not_there_is_named(installed, no_gcloud, tmp_path):
    diagnosis = doctor.diagnose(service_account=str(tmp_path / "absent.json"))
    step = diagnosis.next_step

    assert step.status == doctor.BROKEN
    assert "absent.json" in step.detail


def test_a_token_missing_a_scope_is_not_ready(installed, no_gcloud, monkeypatch):
    monkeypatch.setattr(
        doctor, "load_credentials", lambda **kwargs: _credentials(scopes=[doctor.SCOPES[0]])
    )
    monkeypatch.setattr(doctor, "_check_apis", lambda credentials, gcloud: [])

    diagnosis = doctor.diagnose()
    step = diagnosis.next_step

    assert step.name == "scopes"
    assert "drive.file" in step.detail
    assert step.command == "autowriter setup --login --force"


def test_a_credential_that_cannot_list_its_scopes_is_not_a_failure(installed, no_gcloud, monkeypatch):
    # Service accounts and ADC report None: "whatever the key is entitled to",
    # not "no scopes at all".
    monkeypatch.setattr(doctor, "load_credentials", lambda **kwargs: _credentials(scopes=None))
    monkeypatch.setattr(doctor, "_check_apis", lambda credentials, gcloud: [])

    diagnosis = doctor.diagnose()

    assert diagnosis.ready
    assert "scopes" in _names(diagnosis)


def test_probing_can_be_skipped(installed, no_gcloud, monkeypatch):
    monkeypatch.setattr(doctor, "load_credentials", lambda **kwargs: _credentials())
    monkeypatch.setattr(
        doctor, "_check_apis", _raising(AssertionError("the APIs were probed anyway"))
    )

    diagnosis = doctor.diagnose(probe=False)

    assert diagnosis.ready
    assert diagnosis.checks[-1].status == doctor.SKIPPED


def test_a_complete_setup_reads_as_ready(installed, no_gcloud, monkeypatch):
    monkeypatch.setattr(doctor, "load_credentials", lambda **kwargs: _credentials(scopes=list(doctor.SCOPES)))
    monkeypatch.setattr(
        doctor,
        "_check_apis",
        lambda credentials, gcloud: [doctor.Check("Google Docs API", doctor.OK, "enabled")],
    )

    diagnosis = doctor.diagnose()

    assert diagnosis.ready
    assert diagnosis.next_step is None
    assert "Ready:" in diagnosis.to_text()


# ---------------------------------------------------------------------------
# The API probe
# ---------------------------------------------------------------------------


def _probe_with(error, gcloud=False):
    def call():
        if error is not None:
            raise error
        return {}

    return doctor._probe(
        "Google Docs API", call, "docs.googleapis.com", doctor.CONSOLE_DOCS_API, gcloud
    )


def test_a_probe_that_answers_means_the_api_is_on():
    assert _probe_with(None).status == doctor.OK


def test_the_probe_id_is_meant_to_be_missing():
    # A 404 is the *expected* answer: it proves the API replied.
    assert _probe_with(_HttpError(404)).status == doctor.OK


def test_a_disabled_api_is_reported_with_the_command_that_enables_it():
    error = _HttpError(403, "Google Docs API has not been used in project 12345 before")
    check = _probe_with(error, gcloud=True)

    assert check.status == doctor.MISSING
    assert check.command == "gcloud services enable docs.googleapis.com"


def test_a_disabled_api_without_gcloud_points_at_the_console():
    error = _HttpError(403, "SERVICE_DISABLED")
    assert _probe_with(error).command == doctor.CONSOLE_DOCS_API


def test_a_rejected_credential_is_not_a_disabled_api():
    check = _probe_with(_HttpError(403, "insufficient permissions"))
    assert check.status == doctor.BROKEN


def test_an_unexpected_failure_is_reported_verbatim():
    check = _probe_with(_HttpError(500, "backend error"))
    assert check.status == doctor.BROKEN
    assert "backend error" in check.detail


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


def test_a_diagnosis_never_opens_a_browser(monkeypatch, tmp_path):
    """The regression this whole seam exists for.

    `diagnose` runs from agents and scripts that cannot answer an OAuth
    prompt; a flow started here would block until the caller timed out.
    """
    secrets = tmp_path / "client_secret.json"
    secrets.write_text("{}", encoding="utf-8")

    class _Flow:
        @staticmethod
        def from_client_secrets_file(*args, **kwargs):
            raise AssertionError("the browser flow was started")

    monkeypatch.setattr(auth, "_require", lambda module: _Flow)

    with pytest.raises(auth.AuthError) as error:
        auth.load_credentials(
            client_secrets=str(secrets),
            token_file=str(tmp_path / "token.json"),
            allow_browser=False,
        )
    assert "setup --login" in str(error.value)


def test_login_without_client_secrets_explains_both_routes(installed, gcloud):
    code, message = doctor.login()
    assert code == 2
    assert doctor.GCLOUD_LOGIN in message


def test_login_names_a_client_secrets_file_that_is_not_there(installed, no_gcloud, tmp_path):
    code, message = doctor.login(client_secrets=str(tmp_path / "absent.json"))
    assert code == 2
    assert "absent.json" in message


def test_login_runs_the_flow_and_reports_where_the_token_went(installed, monkeypatch, tmp_path):
    secrets = tmp_path / "client_secret.json"
    secrets.write_text("{}", encoding="utf-8")
    token = tmp_path / "token.json"
    calls = []
    monkeypatch.setattr(doctor, "load_credentials", lambda **kwargs: calls.append(kwargs))

    code, message = doctor.login(client_secrets=str(secrets), token_file=str(token))

    assert code == 0
    assert calls[0]["allow_browser"] is True
    assert str(token) in message


def test_force_discards_the_cached_token_first(installed, monkeypatch, tmp_path):
    secrets = tmp_path / "client_secret.json"
    secrets.write_text("{}", encoding="utf-8")
    token = tmp_path / "token.json"
    token.write_text('{"stale": true}', encoding="utf-8")
    monkeypatch.setattr(doctor, "load_credentials", lambda **kwargs: None)

    doctor.login(client_secrets=str(secrets), token_file=str(token), force=True)

    assert not token.exists()


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def test_setup_needs_no_document(installed, no_gcloud, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "load_credentials", _raising(auth.AuthError("nothing")))
    assert main(["setup"]) == 1
    assert "Google setup" in capsys.readouterr().out


def test_setup_json_is_machine_readable(installed, gcloud, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "load_credentials", _raising(auth.AuthError("nothing")))
    assert main(["setup", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["gcloud"] is True
    assert payload["nextStep"]["command"] == doctor.GCLOUD_LOGIN


def test_setup_exits_zero_when_there_is_nothing_left_to_do(installed, no_gcloud, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "load_credentials", lambda **kwargs: _credentials())
    assert main(["setup", "--no-probe"]) == 0
    assert "Ready:" in capsys.readouterr().out


def _raising(error):
    def raise_it(*args, **kwargs):
        raise error

    return raise_it
