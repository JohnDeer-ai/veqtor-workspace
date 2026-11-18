# SPDX-License-Identifier: Apache-2.0
"""Display-only copies, cache identity, and restrained font observations."""

import hashlib
import json
from zipfile import ZipFile

import pytest
from docx import Document
from lxml import etree as ET
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from veqtor_workspace.checks import W
from veqtor_workspace.preview import display_copy, read_preview
from veqtor_workspace.preview_fonts import font_report


@pytest.mark.parametrize("view,value", [("markup", "1"), ("proposed", "0")])
def test_display_copy_changes_only_view_switches(tmp_path, view, value):
    source, target = tmp_path / "source.docx", tmp_path / "display.docx"
    document = Document()
    paragraph = document.add_paragraph("Keep existing history and comments.")
    revision = ET.SubElement(paragraph._p, W + "ins", {W + "id": "1", W + "author": "Prior author"})
    ET.SubElement(ET.SubElement(revision, W + "r"), W + "t").text = "Original insertion"
    document.save(source)
    before = source.read_bytes()
    display_copy(source, target, view)
    assert source.read_bytes() == before
    with ZipFile(source) as a, ZipFile(target) as b:
        assert a.namelist() == b.namelist()
        for name in a.namelist():
            if name != "word/settings.xml":
                assert a.read(name) == b.read(name)
        old, new = ET.fromstring(a.read("word/settings.xml")), ET.fromstring(b.read("word/settings.xml"))
        setting = new.find(W + "revisionView")
        assert all(setting.get(W + k) == value for k in ("markup", "insDel", "formatting", "comments"))
        new.remove(setting)
        assert ET.tostring(old) == ET.tostring(new)


def test_missing_settings_keeps_existing_markup_preview_usable(tmp_path):
    source, target = tmp_path / "source.docx", tmp_path / "display.docx"
    with ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", b"<document/>")
    display_copy(source, target, "markup")
    with ZipFile(target) as archive:
        assert archive.read("word/document.xml") == b"<document/>"
    with pytest.raises(ValueError, match="settings.xml"):
        display_copy(source, target, "proposed")


def test_cached_view_is_not_silently_relabelled(tmp_path):
    source = tmp_path / "source.docx"
    source.write_bytes(b"unchanged")
    folder = tmp_path / "0123456789ab"
    folder.mkdir()
    receipt = {"status": "rendered", "directory": str(folder), "view": "proposed",
               "files": {side: {"sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
                         for side in ("original", "candidate")}}
    (folder / "preview.json").write_text(json.dumps(receipt))
    assert read_preview(tmp_path, folder.name, source, source)["view"] == "proposed"
    assert read_preview(tmp_path, folder.name, source, source, "proposed")["reused"]
    with pytest.raises(ValueError, match="different view"):
        read_preview(tmp_path, folder.name, source, source, "markup")


def pdf(path, font, text):
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=400)
    spec = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/' + font)})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): spec})})
    stream = DecodedStreamObject()
    stream.set_data(f'BT /F1 12 Tf 20 200 Td ({text}) Tj ET'.encode())
    page[NameObject('/Contents')] = writer._add_object(stream)
    writer.write(path)


@pytest.mark.parametrize("actual,expected", [("ABCDEF+Aptos-Bold", False), ("LiberationSans", True)])
def test_font_difference_requires_corresponding_text(tmp_path, actual, expected):
    source = tmp_path / 'fonts.docx'
    text = 'Unique directly formatted sample text.'
    doc = Document()
    doc.add_paragraph().add_run(text).font.name = 'Aptos'
    doc.styles['Heading 1'].font.name = 'UnusedFont'
    doc.save(source)
    pdf(tmp_path / 'preview.pdf', actual, text)
    result = font_report(source, tmp_path)
    assert result['status'] == 'observed'
    assert result['pdf_fonts'] == [actual.replace('ABCDEF+', '')]
    assert bool(result['possible_substitutions']) == expected
    assert 'UnusedFont' in result['declared_but_not_seen']
    assert not any(x['requested'] == 'UnusedFont' for x in result['possible_substitutions'])
    assert 'not proof' in result['scope']


def test_repeated_or_inherited_font_text_is_not_a_confirmed_substitution(tmp_path):
    source = tmp_path / 'fonts.docx'
    doc = Document()
    text = 'A passage repeated in the same document.'
    for _ in range(2):
        doc.add_paragraph().add_run(text).font.name = 'Aptos'
    doc.styles['Normal'].font.name = 'InheritedFont'
    doc.add_paragraph('Some text inherits its style font.')
    doc.save(source)
    pdf(tmp_path / 'preview.pdf', 'Helvetica', text + ' Some text inherits its style font.')
    result = font_report(source, tmp_path)
    assert result['possible_substitutions'] == []
    (tmp_path / 'preview.pdf').write_bytes(b'broken pdf')
    assert font_report(source, tmp_path)['status'] == 'not_available'
