"""autowriter: copy a .docx into a Google Doc, element by element.

The two entry points worth knowing about:

    from autowriter import read_docx, copy_into_google_doc
"""

from .docxread import read_docx  # noqa: F401
from .ir import Document  # noqa: F401

__all__ = ["read_docx", "Document", "copy_into_google_doc", "__version__"]

__version__ = "0.1.0"


def copy_into_google_doc(path, transport, image_uris=None, options=None):
    """Copy ``path`` into the document behind ``transport``.

    ``transport`` is anything implementing :class:`autowriter.gdocs.builder.DocsTransport`
    — the live API client, or the simulator for a dry run.
    """
    from .gdocs.builder import Copier

    document = read_docx(path)
    return document, Copier(transport, image_uris, options).copy(document)
