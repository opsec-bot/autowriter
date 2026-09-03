"""What is missing before ``copy`` can run, and the command that fixes it.

``copy`` needs four things that ``check`` does not: the Google client
libraries, a credential, the two scopes on that credential, and the Docs and
Drive APIs enabled in whichever project the credential belongs to.  Each of
them otherwise fails in the middle of a copy, as a raw API error, after the
document has already been created.

Nothing here opens a browser or blocks: a diagnosis is safe to run at any
time, including from an agent that cannot answer an OAuth prompt.  Signing in
is a separate, deliberate step -- :func:`login`.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .auth import SCOPES, AuthError, load_credentials

OK = "ok"
MISSING = "missing"
BROKEN = "broken"
SKIPPED = "skipped"

#: Distribution names, so the fix we print is something pip can install.
LIBRARIES = (
    ("googleapiclient", "google-api-python-client"),
    ("google.auth", "google-auth"),
    ("google_auth_oauthlib", "google-auth-oauthlib"),
)

DEFAULT_TOKEN_FILE = "~/.autowriter/token.json"

#: Where `setup --login` tells people to keep the OAuth client, so a printed
#: fix can name a real path instead of a placeholder they have to substitute.
DEFAULT_CLIENT_SECRETS = "~/.autowriter/client_secret.json"

#: A document id that cannot exist.  Asking for it costs nothing and tells us
#: what we want to know: 404 means the API answered, 403 means it is switched
#: off in this project.
PROBE_DOCUMENT_ID = "autowriter-probe-does-not-exist"

CONSOLE_DOCS_API = "https://console.cloud.google.com/apis/library/docs.googleapis.com"
CONSOLE_DRIVE_API = "https://console.cloud.google.com/apis/library/drive.googleapis.com"

#: gcloud refuses an application-default login that does not also request
#: cloud-platform ("scope is required but not requested"), so the command we
#: print has to ask for it -- even though autowriter itself never uses it.
GCLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

GCLOUD_LOGIN = "gcloud auth application-default login --scopes=%s" % ",".join(
    (GCLOUD_SCOPE,) + tuple(SCOPES)
)

SETUP_REFERENCE = "skills/autowriter/reference/google-setup.md"


@dataclass
class Check:
    """One requirement, and -- when it is not met -- how to meet it."""

    name: str
    status: str
    detail: str
    fix: Optional[str] = None
    command: Optional[str] = None

    @property
    def blocking(self) -> bool:
        return self.status in (MISSING, BROKEN)


@dataclass
class Diagnosis:
    checks: List[Check] = field(default_factory=list)
    gcloud: bool = False

    @property
    def ready(self) -> bool:
        return not any(check.blocking for check in self.checks)

    @property
    def next_step(self) -> Optional[Check]:
        """The first thing to fix.

        Order matters: there is no point probing an API without a credential
        to probe it with, so the checks are reported in the order they have to
        be satisfied.
        """
        for check in self.checks:
            if check.blocking:
                return check
        return None

    # -- rendering ---------------------------------------------------------

    def to_dict(self) -> Dict:
        step = self.next_step
        return {
            "ready": self.ready,
            "gcloud": self.gcloud,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status,
                    "detail": check.detail,
                    "fix": check.fix,
                    "command": check.command,
                }
                for check in self.checks
            ],
            "nextStep": None
            if step is None
            else {"name": step.name, "fix": step.fix, "command": step.command},
        }

    def to_text(self) -> str:
        lines = ["Google setup for `autowriter copy`", ""]
        width = max([len(check.name) for check in self.checks] or [0])
        for check in self.checks:
            lines.append(
                "  %-9s %-*s  %s" % ("[%s]" % check.status, width, check.name, check.detail)
            )
        lines.append("")
        step = self.next_step
        if step is None:
            lines.append("Ready: `autowriter copy report.docx` will work.")
        else:
            lines.append("Next: %s" % (step.fix or "resolve the failure above"))
            if step.command:
                for line in step.command.splitlines():
                    lines.append("    %s" % line)
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def missing_libraries() -> List[str]:
    """Distribution names of the client libraries that are not importable."""
    from importlib import util

    absent = []
    for module, distribution in LIBRARIES:
        try:
            found = util.find_spec(module) is not None
        except (ImportError, ValueError):  # a broken or partial install
            found = False
        if not found:
            absent.append(distribution)
    return absent


def has_gcloud() -> bool:
    return shutil.which("gcloud") is not None


def diagnose(
    service_account: Optional[str] = None,
    client_secrets: Optional[str] = None,
    token_file: Optional[str] = None,
    probe: bool = True,
) -> Diagnosis:
    """Work out what still stands between the caller and a working ``copy``."""
    gcloud = has_gcloud()
    diagnosis = Diagnosis(gcloud=gcloud)

    absent = missing_libraries()
    if absent:
        diagnosis.checks.append(
            Check(
                "libraries",
                MISSING,
                "not installed: %s" % ", ".join(absent),
                fix="install the Google client libraries",
                command="pip install \"autowriter[google]\"",
            )
        )
        return diagnosis  # nothing below can be established without them
    diagnosis.checks.append(Check("libraries", OK, "google-api-python-client, google-auth"))

    credentials, credentials_check = _check_credentials(
        service_account, client_secrets, token_file, gcloud
    )
    diagnosis.checks.append(credentials_check)
    if credentials is None:
        return diagnosis

    diagnosis.checks.append(_check_scopes(credentials))

    if not probe:
        diagnosis.checks.append(Check("apis", SKIPPED, "not probed (--no-probe)"))
        return diagnosis
    diagnosis.checks.extend(_check_apis(credentials, gcloud))
    return diagnosis


def _check_credentials(
    service_account: Optional[str],
    client_secrets: Optional[str],
    token_file: Optional[str],
    gcloud: bool,
) -> Tuple[Optional[object], Check]:
    service_account = service_account or os.environ.get("AUTOWRITER_SERVICE_ACCOUNT")
    client_secrets = client_secrets or os.environ.get("AUTOWRITER_CLIENT_SECRETS")
    resolved_token = os.path.normpath(os.path.expanduser(token_file or DEFAULT_TOKEN_FILE))

    named = (("service account key", service_account), ("client secrets", client_secrets))
    for label, path in named:
        if path and not os.path.exists(path):
            return None, Check(
                "credentials",
                BROKEN,
                "%s file not found: %s" % (label, path),
                fix="point at a file that exists, or drop the flag to use another source",
            )

    try:
        # allow_browser=False: a diagnosis must never sit waiting on a sign-in.
        credentials = load_credentials(
            service_account=service_account,
            client_secrets=client_secrets,
            token_file=token_file,
            allow_browser=False,
        )
    except AuthError as error:
        secrets = _known_client_secrets(client_secrets)

        # A cached sign-in that exists but will not refresh is a different
        # problem from having no credential at all, and it has a different
        # fix.  Reporting it as "missing" sends people hunting for a file that
        # is sitting right there.
        if _is_revoked(error):
            return None, Check(
                "credentials",
                BROKEN,
                "the cached sign-in at %s was rejected: the refresh token has "
                "expired or been revoked" % resolved_token,
                fix=_revoked_fix(),
                command=_login_command(secrets, force=True),
            )

        if secrets:
            return None, Check(
                "credentials",
                MISSING,
                "client secrets present, but not signed in yet",
                fix="sign in once; the token is then cached at %s" % resolved_token,
                command=_login_command(secrets),
            )
        return None, Check(
            "credentials",
            MISSING,
            "no service account key, cached sign-in or gcloud login found",
            fix=_credential_fix(gcloud),
            command=_login_command(None),
        )

    if service_account:
        detail = "service account key %s" % service_account
    elif os.path.exists(resolved_token):
        detail = "signed in; token cached at %s" % resolved_token
    else:
        detail = "application default credentials"
    return credentials, Check("credentials", OK, detail)


def _is_revoked(error: AuthError) -> bool:
    """Whether this failure is a cached token that can no longer be refreshed.

    google-auth reports the reason inside an ``invalid_grant`` response rather
    than as a distinct exception type, so the text is what there is to match on.
    """
    message = str(error).lower()
    return "invalid_grant" in message or "could not be refreshed" in message


def _known_client_secrets(client_secrets: Optional[str]) -> Optional[str]:
    """An OAuth client we can name in a printed command, if one exists.

    Falls back to the location `setup --login` writes people towards, so the
    fix for a revoked token is runnable as printed instead of carrying a
    placeholder filename.
    """
    if client_secrets:
        return client_secrets
    default = os.path.normpath(os.path.expanduser(DEFAULT_CLIENT_SECRETS))
    return default if os.path.exists(default) else None


def _login_command(client_secrets: Optional[str], force: bool = False) -> str:
    command = "autowriter setup --login"
    if force:
        command += " --force"
    return command + ' --client-secrets "%s"' % (client_secrets or "client_secret.json")


def _revoked_fix() -> str:
    """Why a working sign-in stops working, and how to stop it recurring.

    Google expires the refresh tokens of an app still in Testing after seven
    days, so a personal-account setup that worked last week fails today for a
    reason nothing local can show.  Publishing the consent screen ends it; the
    app stays unverified and simply keeps warning at sign-in.
    """
    return (
        "sign in again. If this recurs about weekly, the OAuth consent screen is "
        "still in Testing, and Google expires refresh tokens for testing apps "
        "after 7 days -- press Publish app at "
        "https://console.cloud.google.com/apis/credentials/consent to stop it. "
        "The app stays unverified; sign-in keeps showing the unverified warning."
    )


def _credential_fix(gcloud: bool) -> str:
    """How to get a credential.

    Not gcloud, even when gcloud is installed.  ``documents`` is a sensitive
    scope and gcloud's own OAuth client is not verified to request it, so an
    application-default login is refused outright ("This app is blocked") on
    any personal account.  Recommending it would send most people into a wall.
    """
    hint = "create a desktop OAuth client (see %s), then sign in once" % SETUP_REFERENCE
    if gcloud:
        hint += "; on Google Workspace, `%s` is quicker" % GCLOUD_LOGIN
    return hint


def _check_scopes(credentials) -> Check:
    """Verify the credential carries both scopes, where it can be asked.

    Service accounts and ADC report ``None``, which means "whatever the key is
    entitled to" rather than "no scopes" -- so silence is not a failure.
    """
    granted = getattr(credentials, "scopes", None)
    if not granted:
        return Check("scopes", OK, "not enumerable on this credential type")
    absent = [scope for scope in SCOPES if scope not in granted]
    if absent:
        return Check(
            "scopes",
            BROKEN,
            "missing %s" % ", ".join(_short_scope(scope) for scope in absent),
            fix="sign in again to grant both scopes",
            command="autowriter setup --login --force",
        )
    return Check("scopes", OK, ", ".join(_short_scope(scope) for scope in SCOPES))


def _short_scope(scope: str) -> str:
    return scope.rsplit("/", 1)[-1]


def _check_apis(credentials, gcloud: bool) -> List[Check]:
    from .auth import build_services

    try:
        docs_service, drive_service = build_services(credentials)
    except Exception as error:  # pragma: no cover - depends on environment
        return [Check("apis", BROKEN, "could not build the clients (%s)" % _first_line(str(error)))]

    return [
        _probe(
            "Google Docs API",
            lambda: docs_service.documents().get(documentId=PROBE_DOCUMENT_ID).execute(),
            "docs.googleapis.com",
            CONSOLE_DOCS_API,
            gcloud,
        ),
        _probe(
            "Google Drive API",
            lambda: drive_service.about().get(fields="user").execute(),
            "drive.googleapis.com",
            CONSOLE_DRIVE_API,
            gcloud,
        ),
    ]


def _probe(name: str, call: Callable, service: str, console_url: str, gcloud: bool) -> Check:
    try:
        call()
        return Check(name, OK, "enabled")
    except Exception as error:
        status = getattr(getattr(error, "resp", None), "status", None)
        text = str(error)
        if status in (400, 404):
            # The probe asked for something that cannot exist, on purpose: an
            # answer of any kind means the API is switched on and listening.
            return Check(name, OK, "enabled")
        if status == 403 and ("SERVICE_DISABLED" in text or "has not been used in project" in text):
            return Check(
                name,
                MISSING,
                "not enabled in this project",
                fix="enable %s" % service,
                command=("gcloud services enable %s" % service) if gcloud else console_url,
            )
        if status in (401, 403):
            return Check(
                name,
                BROKEN,
                "credential rejected (HTTP %s)" % status,
                fix="check the credential belongs to a project with this API enabled",
            )
        return Check(name, BROKEN, _first_line(text))


def _first_line(text: str) -> str:
    return text.splitlines()[0][:200] if text else "unknown error"


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


def login(
    client_secrets: Optional[str] = None,
    token_file: Optional[str] = None,
    force: bool = False,
) -> Tuple[int, str]:
    """Run the OAuth flow once and cache the token.

    Returns ``(exit code, message)``.  This is the one function here that
    opens a browser and waits for a human, which is why nothing in
    :func:`diagnose` ever reaches it.
    """
    absent = missing_libraries()
    if absent:
        return 2, (
            "Install the Google client libraries first:\n"
            '    pip install "autowriter[google]"'
        )

    client_secrets = client_secrets or os.environ.get("AUTOWRITER_CLIENT_SECRETS")
    resolved_token = os.path.normpath(os.path.expanduser(token_file or DEFAULT_TOKEN_FILE))

    if not client_secrets:
        return 2, _nothing_to_sign_in_with()

    if not os.path.exists(client_secrets):
        return 2, "client secrets file not found: %s" % client_secrets

    if force and os.path.exists(resolved_token):
        os.remove(resolved_token)

    load_credentials(client_secrets=client_secrets, token_file=token_file, allow_browser=True)
    return 0, ("Signed in.  Token cached at %s\nRun `autowriter setup` to confirm." % resolved_token)


def _nothing_to_sign_in_with() -> str:
    lines = ["No OAuth client secrets given, so there is nothing to sign in with."]
    if has_gcloud():
        lines.append("The quickest way in is gcloud, which needs no client of your own:")
        lines.append("    %s" % GCLOUD_LOGIN)
        lines.append("Otherwise create a desktop OAuth client, download its JSON, and pass")
        lines.append("--client-secrets.  See %s" % SETUP_REFERENCE)
    else:
        lines.append("Create a desktop OAuth client, download its JSON, and pass")
        lines.append("--client-secrets.  See %s" % SETUP_REFERENCE)
    return "\n".join(lines)
