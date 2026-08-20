"""The live Docs API transport, plus document creation and image hosting."""

from __future__ import annotations

import io
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..ir import Document, ImageAsset

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5

#: The Docs write quota is enforced per minute (60 writes per user by default),
#: so a rate-limited batch has to wait out a real part of that window.  The
#: second or two that clears a 503 only burns an attempt against a 429.
RATE_LIMIT_DELAY = 20.0
MAX_RATE_LIMIT_DELAY = 60.0

#: The only formats the Docs API will fetch for an inline image.
SUPPORTED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif"}


def document_url(document_id: str) -> str:
    return "https://docs.google.com/document/d/%s/edit" % document_id


class ApiTransport:
    """Implements the transport :mod:`autowriter.gdocs.builder` writes against."""

    def __init__(self, service, document_id: str):
        self.service = service
        self.document_id = document_id
        self.get_calls = 0
        self.batch_calls = 0

    def get_document(self) -> Dict:
        self.get_calls += 1
        return _with_retries(
            lambda: self.service.documents()
            .get(documentId=self.document_id)
            .execute()
        )

    def batch_update(self, requests: Sequence[Dict]) -> List[Dict]:
        if not requests:
            return []
        self.batch_calls += 1
        response = _with_retries(
            lambda: self.service.documents()
            .batchUpdate(documentId=self.document_id, body={"requests": list(requests)})
            .execute()
        )
        return response.get("replies", [{}] * len(requests))


def create_document(service, title: str) -> str:
    response = _with_retries(lambda: service.documents().create(body={"title": title}).execute())
    return response["documentId"]


def _with_retries(call, attempts: int = MAX_ATTEMPTS, sleep=time.sleep):
    """Retry the transient failures the Docs API is prone to under load."""
    delay = 1.0
    last_error = None
    for attempt in range(attempts):
        try:
            return call()
        except Exception as error:  # googleapiclient raises HttpError
            status = getattr(getattr(error, "resp", None), "status", None)
            if status not in RETRY_STATUSES or attempt == attempts - 1:
                raise
            last_error = error
            sleep(_backoff(status, attempt, delay) + random.uniform(0, 0.4))
            delay *= 2
    raise last_error  # pragma: no cover - unreachable


def _backoff(status: Optional[int], attempt: int, delay: float) -> float:
    """How long to wait before retrying, in seconds."""
    if status == 429:
        return min(RATE_LIMIT_DELAY * (2 ** attempt), MAX_RATE_LIMIT_DELAY)
    return delay


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


@dataclass
class HostedImages:
    """Images parked on Drive so the Docs API can fetch them.

    ``insertInlineImage`` takes a URL, not bytes, and the URL has to be
    publicly readable for Google's fetcher.  So each image is uploaded, shared
    with "anyone with the link", inserted (Docs copies it into the document at
    that moment) and then deleted again.
    """

    drive: object
    uris: Dict[str, str] = field(default_factory=dict)
    file_ids: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    keep: bool = False

    def upload(self, assets: Dict[str, ImageAsset]) -> Dict[str, str]:
        http = _media_module()
        for asset_id, asset in assets.items():
            data, content_type = _as_supported_image(asset, self.notes)
            if data is None:
                continue
            media = http.MediaIoBaseUpload(
                io.BytesIO(data), mimetype=content_type, resumable=False
            )
            created = _with_retries(
                lambda: self.drive.files()
                .create(
                    body={"name": "autowriter-%s.%s" % (asset_id, asset.extension)},
                    media_body=media,
                    fields="id",
                )
                .execute()
            )
            file_id = created["id"]
            self.file_ids.append(file_id)
            _with_retries(
                lambda: self.drive.permissions()
                .create(fileId=file_id, body={"type": "anyone", "role": "reader"})
                .execute()
            )
            self.uris[asset_id] = "https://drive.google.com/uc?export=download&id=%s" % file_id
        return self.uris

    def cleanup(self) -> None:
        if self.keep:
            return
        for file_id in self.file_ids:
            try:
                _with_retries(lambda: self.drive.files().delete(fileId=file_id).execute())
            except Exception:  # best effort; a leftover temp file is not fatal
                self.notes.append(
                    "temporary image file %s could not be deleted from Drive" % file_id
                )
        self.file_ids = []


def _media_module():
    import googleapiclient.http as http  # local import: optional dependency

    return http


def _as_supported_image(asset: ImageAsset, notes: List[str]):
    """Return image bytes the Docs API will accept, converting if it can."""
    content_type = (asset.content_type or "").lower()
    if content_type in SUPPORTED_IMAGE_TYPES:
        return asset.data, content_type
    try:
        from PIL import Image  # optional dependency
    except ImportError:
        notes.append(
            "an image of type %r was skipped; Google Docs accepts only PNG, JPEG and GIF "
            "(install Pillow to have them converted automatically)" % (content_type or "unknown")
        )
        return None, None
    try:
        with Image.open(io.BytesIO(asset.data)) as image:
            buffer = io.BytesIO()
            image.convert("RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB").save(
                buffer, format="PNG"
            )
        notes.append("an image of type %r was converted to PNG" % (content_type or "unknown"))
        return buffer.getvalue(), "image/png"
    except Exception as error:
        notes.append("an image of type %r could not be converted (%s)" % (content_type, error))
        return None, None


def collect_image_uris(document: Document, drive, keep: bool = False) -> HostedImages:
    hosted = HostedImages(drive=drive, keep=keep)
    if document.assets:
        hosted.upload(document.assets)
    return hosted
