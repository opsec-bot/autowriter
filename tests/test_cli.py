"""The command line: exit codes, report contents, and the JSON shape."""

from __future__ import annotations

import json

import pytest

from autowriter.cli import main
from autowriter.report import FidelityReport
from autowriter.ir import Note

from .fixtures import SECTION, build_docx, paragraph
from .test_reader import cell_xml, list_paragraph, row_xml, table_xml


@pytest.fixture
def sample(tmp_path):
    body = (
        paragraph("Report", style="Heading1")
        + paragraph("Some body text.")
        + list_paragraph("a bullet", 0, "1")
        + table_xml([row_xml([cell_xml("k"), cell_xml("v")])])
        + SECTION
    )
    path = tmp_path / "sample.docx"
    path.write_bytes(build_docx(body).getvalue())
    return str(path)


def test_check_reports_a_clean_copy(sample, capsys):
    assert main(["check", sample]) == 0
    output = capsys.readouterr().out
    assert "every paragraph, character and property matches" in output
    # three body paragraphs plus the two inside the table
    assert "5 paragraphs" in output


def test_check_json_is_machine_readable(sample, capsys):
    assert main(["check", sample, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verified"] is True
    assert payload["counts"]["tables"] == 1
    assert payload["differences"] == []


def test_inspect_prints_the_structure(sample, capsys):
    assert main(["inspect", sample]) == 0
    output = capsys.readouterr().out
    assert "HEADING_1" in output
    assert "Table 1x2" in output
    assert "list=bullet/L0" in output


def test_plan_emits_valid_requests(sample, capsys):
    assert main(["plan", sample]) == 0
    requests = json.loads(capsys.readouterr().out)
    assert all(len(request) == 1 for request in requests)
    assert any("insertText" in request for request in requests)
    assert any("createParagraphBullets" in request for request in requests)


def test_missing_file_is_an_error(capsys):
    assert main(["check", "/nonexistent/file.docx"]) == 2
    assert "no such file" in capsys.readouterr().err


def test_report_lists_notes_by_kind():
    report = FidelityReport(source="x.docx")
    report.add_notes([Note("unsupported", "comments are not carried over")])
    report.add_notes([Note("approximation", "a floating image was placed inline")])
    text = report.to_text()
    assert "Not reproducible in Google Docs" in text
    assert "Copied approximately" in text
    assert "comments are not carried over" in text


def test_report_deduplicates_notes():
    report = FidelityReport(source="x.docx")
    report.add_notes([Note("unsupported", "same")])
    report.add_notes([Note("unsupported", "same")])
    assert len(report.notes) == 1


def test_check_fails_when_the_source_cannot_be_matched(tmp_path, capsys, monkeypatch):
    # A copy that silently drops content must not report success.
    body = paragraph("kept") + paragraph("dropped") + SECTION
    path = tmp_path / "d.docx"
    path.write_bytes(build_docx(body).getvalue())

    from autowriter.gdocs import builder

    original = builder.SegmentWriter.write_paragraph

    def skip_second(self, paragraph_ir):
        if paragraph_ir.text == "dropped":
            return
        return original(self, paragraph_ir)

    monkeypatch.setattr(builder.SegmentWriter, "write_paragraph", skip_second)
    assert main(["check", str(path)]) == 1
    assert "difference" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# copy: the one command that talks to Google
# ---------------------------------------------------------------------------


IMAGE_PARAGRAPH = (
    "<w:p><w:r><w:drawing><wp:inline>"
    '<wp:extent cx="914400" cy="457200"/>'
    '<wp:docPr id="1" name="Picture 1"/>'
    '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rIdImg"/>'
    "</pic:blipFill></pic:pic></a:graphicData></a:graphic>"
    "</wp:inline></w:drawing></w:r></w:p>"
)

IMAGE_REL = (
    '<Relationship Id="rIdImg" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
    'Target="media/image1.png"/>'
)


@pytest.fixture
def illustrated(tmp_path):
    from .fixtures import PNG_BYTES

    body = paragraph("With a picture") + IMAGE_PARAGRAPH + SECTION
    path = tmp_path / "illustrated.docx"
    path.write_bytes(
        build_docx(
            body,
            extra_parts={"word/media/image1.png": PNG_BYTES},
            extra_rels=IMAGE_REL,
        ).getvalue()
    )
    return str(path)


@pytest.fixture
def fake_google(monkeypatch):
    """Wire `copy` up to the simulator and a Drive that records what it is told."""
    from autowriter.gdocs import auth, client as api
    from autowriter.gdocs.simulator import SimulatedDocs

    from .test_client import _FakeDrive, _Media

    drive = _FakeDrive()
    monkeypatch.setattr(auth, "load_credentials", lambda **kwargs: object())
    monkeypatch.setattr(auth, "build_services", lambda credentials: (object(), drive))
    monkeypatch.setattr(api, "create_document", lambda service, title: "doc-1")
    monkeypatch.setattr(api, "_media_module", lambda: _Media)
    monkeypatch.setattr(api, "ApiTransport", lambda service, document_id: SimulatedDocs())
    return drive


def test_copy_reports_the_document_url(sample, fake_google, capsys):
    assert main(["copy", sample]) == 0
    assert "https://docs.google.com/document/d/doc-1/edit" in capsys.readouterr().out


def test_copy_removes_the_temporary_image_uploads(illustrated, fake_google, capsys):
    assert main(["copy", illustrated]) == 0
    assert fake_google.uploaded, "the image should have been hosted for Docs to fetch"
    assert fake_google.deleted == fake_google.uploaded


def test_a_failure_during_the_copy_still_removes_the_uploads(
    illustrated, fake_google, monkeypatch
):
    # The images are shared with anyone holding the link for as long as they
    # exist, so no error path may leave them behind.
    from autowriter import cli

    class _Exploding:
        def __init__(self, *args, **kwargs):
            pass

        def copy(self, document):
            raise RuntimeError("boom")

    monkeypatch.setattr(cli, "Copier", _Exploding)
    with pytest.raises(RuntimeError):
        main(["copy", illustrated])
    assert fake_google.deleted == fake_google.uploaded
    assert fake_google.deleted


def test_keeping_the_uploads_leaves_them_in_place(illustrated, fake_google, capsys):
    assert main(["copy", illustrated, "--keep-image-uploads"]) == 0
    assert fake_google.uploaded
    assert fake_google.deleted == []
