"""Retry behaviour for the live transport."""

import pytest

from autowriter.gdocs import client as api


class _Response:
    def __init__(self, status):
        self.status = status


class _HttpError(Exception):
    def __init__(self, status):
        super().__init__("HTTP %d" % status)
        self.resp = _Response(status)


def _failing(statuses, result="ok"):
    """A call that raises the given statuses in turn, then succeeds."""
    remaining = list(statuses)

    def call():
        if remaining:
            raise _HttpError(remaining.pop(0))
        return result

    return call


def test_transient_failures_are_retried():
    waits = []
    assert api._with_retries(_failing([503, 500]), sleep=waits.append) == "ok"
    assert len(waits) == 2


def test_a_rate_limit_waits_out_the_quota_window():
    # The write quota is per minute, so a one-second backoff would exhaust every
    # attempt inside the same window and fail a copy that only needed to wait.
    waits = []
    assert api._with_retries(_failing([429]), sleep=waits.append) == "ok"
    assert waits[0] >= api.RATE_LIMIT_DELAY


def test_rate_limit_backoff_grows_but_stays_bounded():
    waits = []
    api._with_retries(_failing([429, 429, 429]), sleep=waits.append)
    assert waits[1] > waits[0]
    assert max(waits) <= api.MAX_RATE_LIMIT_DELAY + 1


def test_a_permanent_failure_is_not_retried():
    waits = []
    with pytest.raises(_HttpError):
        api._with_retries(_failing([400]), sleep=waits.append)
    assert waits == []


def test_retries_give_up_and_raise_the_last_error():
    waits = []
    with pytest.raises(_HttpError):
        api._with_retries(_failing([503] * api.MAX_ATTEMPTS), sleep=waits.append)
    assert len(waits) == api.MAX_ATTEMPTS - 1


# ---------------------------------------------------------------------------
# Hosted images
# ---------------------------------------------------------------------------


class _Call:
    """A pending API call: ``.execute()`` runs it, as googleapiclient does."""

    def __init__(self, run):
        self.execute = run


class _FakeDrive:
    """Enough of the Drive client to count uploads, shares and deletions."""

    def __init__(self, fail_permission_on=None):
        self.uploaded = []
        self.shared = []
        self.deleted = []
        self._fail_permission_on = fail_permission_on

    def files(self):
        return self

    def permissions(self):
        return _Permissions(self)

    def create(self, body=None, media_body=None, fields=None):
        def run():
            file_id = "file-%d" % (len(self.uploaded) + 1)
            self.uploaded.append(file_id)
            return {"id": file_id}

        return _Call(run)

    def delete(self, fileId=None):
        return _Call(lambda: self.deleted.append(fileId))


class _Permissions:
    def __init__(self, drive):
        self.drive = drive

    def create(self, fileId=None, body=None):
        def run():
            if fileId == self.drive._fail_permission_on:
                raise _HttpError(403)
            self.drive.shared.append(fileId)
            return {}

        return _Call(run)


class _Media:
    """Stands in for googleapiclient.http, which is an optional dependency."""

    @staticmethod
    def MediaIoBaseUpload(fileobj, mimetype=None, resumable=False):
        return fileobj


def _png(asset_id):
    from autowriter.ir import ImageAsset

    return ImageAsset(
        asset_id=asset_id, data=b"\x89PNG", content_type="image/png", extension="png"
    )


@pytest.fixture
def fake_media(monkeypatch):
    monkeypatch.setattr(api, "_media_module", lambda: _Media)


def test_images_are_uploaded_shared_and_cleaned_up(fake_media):
    drive = _FakeDrive()
    hosted = api.HostedImages(drive=drive)
    uris = hosted.upload({"a": _png("a"), "b": _png("b")})

    assert len(uris) == 2
    assert drive.shared == drive.uploaded
    hosted.cleanup()
    assert drive.deleted == drive.uploaded


def test_a_failed_upload_deletes_the_images_it_already_shared(fake_media):
    # Every hosted image is readable by anyone with the link until it is
    # deleted.  If the run dies half way through, the ones already uploaded
    # have to go with it -- nothing else will ever come back for them.
    drive = _FakeDrive(fail_permission_on="file-2")
    hosted = api.HostedImages(drive=drive)

    with pytest.raises(_HttpError):
        hosted.upload({"a": _png("a"), "b": _png("b"), "c": _png("c")})

    assert drive.uploaded == ["file-1", "file-2"]
    assert sorted(drive.deleted) == ["file-1", "file-2"]


def test_an_interrupted_upload_still_cleans_up(fake_media, monkeypatch):
    drive = _FakeDrive()
    hosted = api.HostedImages(drive=drive)

    def interrupt(assets):
        hosted.file_ids.append("file-1")
        drive.uploaded.append("file-1")
        raise KeyboardInterrupt

    monkeypatch.setattr(hosted, "_upload", interrupt)
    with pytest.raises(KeyboardInterrupt):
        hosted.upload({"a": _png("a")})
    assert drive.deleted == ["file-1"]


def test_keeping_the_uploads_is_opt_in(fake_media):
    drive = _FakeDrive()
    hosted = api.HostedImages(drive=drive, keep=True)
    hosted.upload({"a": _png("a")})
    hosted.cleanup()
    assert drive.deleted == []
