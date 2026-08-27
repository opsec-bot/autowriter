"""Credentials for the Docs and Drive APIs.

Three ways in, tried in the order they are given on the command line:

* a service account key file (headless; the target Drive must be shared with
  the service account, or domain-wide delegation configured)
* an installed-app OAuth client, cached in a token file after the first run
* application default credentials (``gcloud auth application-default login``)

The Google client libraries are an optional dependency: everything except the
``copy`` command works without them, so they are imported lazily and produce a
readable error rather than an ImportError traceback.
"""

from __future__ import annotations

import os
from typing import List, Optional, Sequence

#: ``documents`` to write the copy, ``drive.file`` to create it and to host the
#: images long enough for the Docs API to fetch them.  ``drive.file`` only ever
#: grants access to files this tool itself created.
SCOPES: Sequence[str] = (
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
)

INSTALL_HINT = (
    "The Google API client libraries are required for this command.\n"
    "    pip install google-api-python-client google-auth google-auth-oauthlib"
)


class AuthError(RuntimeError):
    pass


def _require(module: str):
    try:
        return __import__(module, fromlist=["*"])
    except ImportError as error:  # pragma: no cover - depends on environment
        raise AuthError("%s\n(%s)" % (INSTALL_HINT, error)) from error


def load_credentials(
    service_account: Optional[str] = None,
    client_secrets: Optional[str] = None,
    token_file: Optional[str] = None,
    scopes: Optional[Sequence[str]] = None,
):
    """Return credentials, preferring whichever source the caller configured."""
    scopes = list(scopes or SCOPES)
    service_account = service_account or os.environ.get("AUTOWRITER_SERVICE_ACCOUNT")
    client_secrets = client_secrets or os.environ.get("AUTOWRITER_CLIENT_SECRETS")

    if service_account:
        service_account_module = _require("google.oauth2.service_account")
        return service_account_module.Credentials.from_service_account_file(
            service_account, scopes=scopes
        )

    if client_secrets:
        return _installed_app_flow(client_secrets, token_file, scopes)

    google_auth = _require("google.auth")
    try:
        credentials, _project = google_auth.default(scopes=scopes)
        return credentials
    except Exception as error:  # pragma: no cover - depends on environment
        raise AuthError(
            "No credentials found.  Pass --service-account or --client-secrets, "
            "or run 'gcloud auth application-default login'.\n(%s)" % error
        ) from error


def _installed_app_flow(client_secrets: str, token_file: Optional[str], scopes: List[str]):
    token_file = token_file or os.path.expanduser("~/.autowriter/token.json")
    credentials_module = _require("google.oauth2.credentials")
    request_module = _require("google.auth.transport.requests")
    flow_module = _require("google_auth_oauthlib.flow")

    credentials = None
    if os.path.exists(token_file):
        credentials = credentials_module.Credentials.from_authorized_user_file(token_file, scopes)
    if credentials is None or not credentials.valid:
        if credentials is not None and credentials.expired and credentials.refresh_token:
            credentials.refresh(request_module.Request())
        else:
            flow = flow_module.InstalledAppFlow.from_client_secrets_file(client_secrets, scopes)
            credentials = flow.run_local_server(port=0)
        _write_token(token_file, credentials.to_json())
    return credentials


def _write_token(path: str, contents: str) -> None:
    """Cache the token, readable only by its owner.

    Created with the restrictive mode rather than chmod'ed afterwards: the file
    holds a refresh token, and between ``open`` and ``chmod`` it would be
    readable by anyone the umask allows.
    """
    directory = os.path.dirname(path)
    if directory:  # a bare filename has no directory to create
        os.makedirs(directory, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(contents)
    os.chmod(path, 0o600)  # an existing file keeps its old mode through O_CREAT


def build_services(credentials):
    """Build the Docs and Drive service clients."""
    discovery = _require("googleapiclient.discovery")
    docs = discovery.build("docs", "v1", credentials=credentials, cache_discovery=False)
    drive = discovery.build("drive", "v3", credentials=credentials, cache_discovery=False)
    return docs, drive
