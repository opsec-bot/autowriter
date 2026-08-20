"""Command line interface.

    autowriter check  report.docx            # offline dry run + fidelity report
    autowriter copy   report.docx            # write it into a new Google Doc
    autowriter plan   report.docx            # dump the batchUpdate requests
    autowriter inspect report.docx           # dump the parsed document
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from . import ir
from .docxread import read_docx
from .gdocs.builder import Copier, CopyOptions
from .gdocs.simulator import SimulatedDocs
from .gdocs.verify import verify
from .report import FidelityReport, count_content


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autowriter",
        description="Recreate a .docx inside a Google Doc, element by element.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("docx", help="path to the .docx file")
        sub.add_argument(
            "--keep-hidden",
            action="store_true",
            help="copy runs Word marks as hidden (they are skipped by default)",
        )
        sub.add_argument(
            "--literal-caps",
            action="store_true",
            help='keep the stored casing of "All caps" text instead of upper-casing it',
        )
        sub.add_argument("--no-headers", action="store_true", help="skip headers and footers")
        sub.add_argument("--no-footnotes", action="store_true", help="skip footnotes")

    check = commands.add_parser(
        "check",
        help="copy into an in-memory Google Docs model and report fidelity (no network)",
    )
    common(check)
    check.add_argument("--json", action="store_true", help="emit the report as JSON")

    copy = commands.add_parser("copy", help="copy the document into a real Google Doc")
    common(copy)
    copy.add_argument("--title", help="title for the new document (default: the .docx title)")
    copy.add_argument(
        "--document-id",
        help="write into this existing (and preferably empty) document instead of creating one",
    )
    copy.add_argument("--service-account", help="path to a service account key file")
    copy.add_argument("--client-secrets", help="path to an OAuth client secrets file")
    copy.add_argument("--token-file", help="where to cache the OAuth token")
    copy.add_argument("--no-images", action="store_true", help="skip images entirely")
    copy.add_argument(
        "--keep-image-uploads",
        action="store_true",
        help="leave the temporary Drive copies of images in place",
    )
    copy.add_argument("--no-verify", action="store_true", help="skip reading the result back")
    copy.add_argument("--json", action="store_true", help="emit the report as JSON")

    plan = commands.add_parser("plan", help="print the batchUpdate requests as JSON")
    common(plan)

    inspect = commands.add_parser("inspect", help="print the parsed document structure")
    common(inspect)
    return parser


def copy_options(args: argparse.Namespace) -> CopyOptions:
    return CopyOptions(
        render_all_caps=not args.literal_caps,
        include_hidden_text=args.keep_hidden,
        copy_headers_footers=not args.no_headers,
        copy_footnotes=not args.no_footnotes,
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not os.path.exists(args.docx):
        print("no such file: %s" % args.docx, file=sys.stderr)
        return 2

    if args.command == "inspect":
        return _inspect(args)
    if args.command == "plan":
        return _plan(args)
    if args.command == "check":
        return _check(args)
    return _copy(args)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _check(args: argparse.Namespace) -> int:
    document = read_docx(args.docx)
    options = copy_options(args)
    simulator = SimulatedDocs(title=document.title or os.path.basename(args.docx))
    # Images are only reachable by URL from Google's side, so the dry run uses
    # placeholders that keep the index arithmetic honest.
    image_uris = {asset_id: "https://example.invalid/%s" % asset_id for asset_id in document.assets}
    result = Copier(simulator, image_uris, options).copy(document)
    verification = verify(document, simulator.get_document(), options)

    report = _make_report(args.docx, document, result, None)
    report.characters = verification.characters_checked
    report.verified = verification.ok
    report.differences = [str(difference) for difference in verification.differences]

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.to_text())
    return 0 if verification.ok else 1


def _copy(args: argparse.Namespace) -> int:
    from .gdocs import client as api
    from .gdocs.auth import AuthError, build_services, load_credentials

    document = read_docx(args.docx)
    options = copy_options(args)
    try:
        credentials = load_credentials(
            service_account=args.service_account,
            client_secrets=args.client_secrets,
            token_file=args.token_file,
        )
        docs_service, drive_service = build_services(credentials)
    except AuthError as error:
        print(str(error), file=sys.stderr)
        return 2

    title = args.title or document.title or os.path.splitext(os.path.basename(args.docx))[0]
    document_id = args.document_id or api.create_document(docs_service, title)

    hosted = None
    image_uris = {}
    if document.assets and not args.no_images:
        hosted = api.collect_image_uris(document, drive_service, keep=args.keep_image_uploads)
        image_uris = hosted.uris

    transport = api.ApiTransport(docs_service, document_id)
    try:
        result = Copier(transport, image_uris, options).copy(document)
    finally:
        if hosted is not None:
            hosted.cleanup()

    report = _make_report(args.docx, document, result, api.document_url(document_id))
    if hosted is not None:
        report.add_messages("unsupported", hosted.notes)

    if not args.no_verify:
        verification = verify(document, transport.get_document(), options)
        report.verified = verification.ok
        report.characters = verification.characters_checked
        report.differences = [str(difference) for difference in verification.differences]

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.to_text())
    return 0 if report.verified is not False else 1


def _plan(args: argparse.Namespace) -> int:
    document = read_docx(args.docx)
    simulator = SimulatedDocs()
    image_uris = {asset_id: "https://example.invalid/%s" % asset_id for asset_id in document.assets}
    Copier(simulator, image_uris, copy_options(args)).copy(document)
    print(json.dumps(simulator.applied, indent=2, ensure_ascii=False))
    return 0


def _inspect(args: argparse.Namespace) -> int:
    document = read_docx(args.docx)
    for section_index, section in enumerate(document.sections):
        print("Section %d  %s" % (section_index + 1, section.props))
        for name, header in section.headers.items():
            print("  header:%s  %d block(s)" % (name, len(header.blocks)))
        for name, footer in section.footers.items():
            print("  footer:%s  %d block(s)" % (name, len(footer.blocks)))
        _print_blocks(section.blocks, indent=2)
    if document.assets:
        print("\nImages")
        for asset in document.assets.values():
            print("  %s  %s  %d bytes" % (asset.asset_id, asset.content_type, len(asset.data)))
    if document.notes:
        print("\nNotes")
        for note in document.notes:
            print("  %s" % note)
    return 0


def _print_blocks(blocks, indent: int) -> None:
    pad = " " * indent
    for block in blocks:
        if isinstance(block, ir.Table):
            print("%sTable %dx%d" % (pad, len(block.rows), block.column_count))
            for row_index, row in enumerate(block.rows):
                for cell_index, cell in enumerate(row.cells):
                    marker = " (merged away)" if cell.merged_away else ""
                    print("%s  r%dc%d%s" % (pad, row_index, cell_index, marker))
                    _print_blocks(cell.blocks, indent + 4)
            continue
        marker = ""
        if block.list_marker is not None:
            marker = "  list=%s/L%d" % (block.list_marker.number_format, block.list_marker.level)
        print(
            "%s%s%s  %r"
            % (pad, block.props.named_style, marker, block.text[:70])
        )


def _make_report(source: str, document: ir.Document, result, url: Optional[str]) -> FidelityReport:
    counts = count_content(document)
    report = FidelityReport(source=source, document_url=url)
    report.paragraphs = counts["paragraphs"]
    report.tables = counts["tables"]
    report.images = counts["images"]
    report.requests = result.request_count
    report.batches = result.batch_count
    report.add_notes(result.notes)
    return report


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
