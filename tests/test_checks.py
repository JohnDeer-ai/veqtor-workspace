# SPDX-License-Identifier: Apache-2.0
"""Checker controls on small synthetic documents: Word-native edits pass, defects are reported."""

from zipfile import ZipFile

import pytest
from veqtor_workspace.checks import check_docx

NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
      'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml" '
      'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"')
CT = b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
D, ME, OTHER = "2026-01-01T00:00:00Z", "Ilya", "Other"


def run(text, tag="t", rpr="", space=True):
    preserve = ' xml:space="preserve"' if space else ""
    return f'<w:r>{rpr}<w:{tag}{preserve}>{text}</w:{tag}></w:r>'


def math(text):
    return f"<m:oMath><m:r><m:t>{text}</m:t></m:r></m:oMath>"


def rev(kind, rid, author, *content, date=D):
    stamp = f' w:date="{date}"' if date else ""
    return f'<w:{kind} w:id="{rid}" w:author="{author}"{stamp}>{"".join(content)}</w:{kind}>'


def mark(kind, rid, author=ME):
    """Paragraph properties carrying an inserted or deleted paragraph mark."""
    return f"<w:pPr><w:rPr>{rev(kind, rid, author)}</w:rPr></w:pPr>"


def para(*content, ppr=""):
    return f"<w:p>{ppr}{''.join(content)}</w:p>"


def docx(path, body, parts=None):
    files = {"[Content_Types].xml": CT,
             "word/document.xml": f"<w:document {NS}><w:body>{''.join(body)}</w:body></w:document>".encode()}
    for name, xml in (parts or {}).items():
        files[name] = xml.encode() if isinstance(xml, str) else xml
    with ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def check(tmp_path, original, candidate, original_parts=None, candidate_parts=None):
    report = check_docx(docx(tmp_path / "original.docx", original, original_parts),
                        docx(tmp_path / "candidate.docx", candidate, candidate_parts), ME)
    return {c["check"] for c in report["checks"] if c["status"] == "failed"}, report


CELL = '<w:tcPr><w:tcW w:w="100" w:type="dxa"/>{change}</w:tcPr>'
TABLE = '<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid><w:gridCol w:w="100"/></w:tblGrid>{rows}</w:tbl>'
ROW = "<w:tr><w:tc>" + CELL.format(change="") + para(run("Kept")) + "</w:tc></w:tr>"
TRACKED_CELL_ROW = ("<w:tr><w:tc>" + CELL.format(change=rev(
    "tcPrChange", "5", OTHER, '<w:tcPr><w:tcW w:w="50" w:type="dxa"/></w:tcPr>')) + para(run("Kept")) + "</w:tc></w:tr>")
NEW_ROW = (f'<w:tr><w:trPr>{rev("ins", "3", ME)}</w:trPr><w:tc>' + CELL.format(change="")
           + para(rev("ins", "4", ME, run("Added row")), ppr=mark("ins", "5")) + "</w:tc></w:tr>")
TRACKED_BOLD = f'<w:rPr><w:b/>{rev("rPrChange", "1", ME, "<w:rPr/>")}</w:rPr>'

NATIVE = {
    "replace": (
        [para(run("Pay within 10 days"))],
        [para(run("Pay within "), rev("del", "1", ME, run("10", "delText")), rev("ins", "2", ME, run("20")), run(" days"))]),
    "counter_whole_insertion": (
        [para(run("paid by "), rev("ins", "5", OTHER, run("Right Holder")))],
        [para(run("paid by "), rev("ins", "5", OTHER, rev("del", "6", ME, run("Right Holder", "delText"))),
              rev("ins", "7", ME, run("Licensee")))]),
    "counter_middle_of_insertion": (
        [para(rev("ins", "5", OTHER, run("the Right Holder shall pay")))],
        [para(rev("ins", "5", OTHER, run("the ")), rev("ins", "8", OTHER, rev("del", "6", ME, run("Right Holder", "delText"))),
              rev("ins", "7", ME, run("Licensee")), rev("ins", "9", OTHER, run(" shall pay")))]),
    "restore_deletion": (
        [para(run("A "), rev("del", "5", OTHER, run("B", "delText")), run(" C"))],
        [para(run("A "), rev("del", "5", OTHER, run("B", "delText")), rev("ins", "6", ME, run("B")), run(" C"))]),
    "new_paragraph_before": (
        [para(run("First")), para(run("Second"))],
        [para(run("First")), para(rev("ins", "2", ME, run("New")), ppr=mark("ins", "1")), para(run("Second"))]),
    "new_paragraph_at_end": (
        [para(run("Only"))],
        [para(run("Only"), ppr=mark("ins", "1")), para(rev("ins", "2", ME, run("Added")))]),
    "delete_paragraph": (
        [para(run("Gone")), para(run("Stays"))],
        [para(rev("del", "1", ME, run("Gone", "delText")), ppr=mark("del", "2")), para(run("Stays"))]),
    "tracked_bold": ([para(run("Bold me"))], [para(run("Bold me", rpr=TRACKED_BOLD))]),
    "new_table_row": ([TABLE.format(rows=ROW)], [TABLE.format(rows=ROW + NEW_ROW)]),
    "harmless_run_split": ([para(run("Hello world"))], [para(run("Hello "), '<w:proofErr w:type="spellStart"/>', run("world"))]),
    "runs_merged_without_preserve": ([para(run("Hello"), run(" world"))], [para(run("Hello world", space=False))]),
    "pretty_printed_xml": (
        [para(run("Text"), math("10000"))],
        [para("\n  ", run("Text"), "\n  ", "<m:oMath>\n    <m:r>\n      <m:t>10000</m:t>\n    </m:r>\n  </m:oMath>", "\n")]),
}


@pytest.mark.parametrize("name", sorted(NATIVE))
def test_word_native_edits_pass(tmp_path, name):
    original, candidate = NATIVE[name]
    failed, report = check(tmp_path, original, candidate)
    assert not failed, report
    assert report["status"] == "checked_with_limits"


DEFECTS = {
    "untracked_text": ([para(run("Pay within 10 days"))], [para(run("Pay within 70 days"))], "untracked_changes"),
    "untracked_number_beside_new_paragraph": (
        [para(run("Pay within 10 days")), para(run("Second"))],
        [para(run("Pay within 70 days")), para(rev("ins", "2", ME, run("New")), ppr=mark("ins", "1")), para(run("Second"))],
        "untracked_changes"),
    "untracked_number_beside_tracked_bold": (
        [para(run("Bold me")), para(run("Pay within 10 days"))],
        [para(run("Bold me", rpr=TRACKED_BOLD)), para(run("Pay within 70 days"))], "untracked_changes"),
    "untracked_bold": ([para(run("Plain"))], [para(run("Plain", rpr="<w:rPr><w:b/></w:rPr>"))], "untracked_changes"),
    "wrong_author": (
        [para(run("Old"))], [para(rev("del", "1", OTHER, run("Old", "delText")), rev("ins", "2", OTHER, run("New")))],
        "new_revision_authors"),
    "missing_date": ([para(run("Old"))], [para(run("Old"), rev("ins", "1", ME, run(" more"), date=None))], "new_revision_dates"),
    "reused_id": (
        [para(run("Old"))], [para(run("Old"), rev("ins", "1", ME, run(" a")), rev("ins", "1", ME, run(" b")))], "new_revision_ids"),
    # The three structures below open in Word 16.95 only as a repaired copy.
    "insertion_inside_their_deletion": (
        [para(rev("del", "5", OTHER, run("Gone", "delText")))],
        [para(rev("del", "5", OTHER, run("Go", "delText"), rev("ins", "6", ME, run("Mine")), run("ne", "delText")))],
        "new_revision_structure"),
    "plain_text_inside_new_deletion": ([para(run("Old"))], [para(rev("del", "1", ME, run("Old")))], "new_revision_structure"),
    "deleted_text_inside_new_insertion": (
        [para(run("Old"))], [para(run("Old"), rev("ins", "1", ME, run(" more", "delText")))], "new_revision_structure"),
    "untracked_formula_number": (
        [para(run("Amount "), math("10000"))], [para(run("Amount "), math("90000"))], "untracked_changes"),
    "significant_space_dropped": (
        [para(run("Pay within"), run(" 10 days"))], [para(run("Pay within"), run(" 10 days", space=False))],
        "untracked_changes"),
    "split_without_preserve": (
        [para(run("Pay within 10 days"))],
        [para(run("Pay within ", space=False), rev("del", "1", ME, run("10", "delText")), rev("ins", "2", ME, run("20")),
              run(" days", space=False))], "untracked_changes"),
    "their_insertion_edited": (
        [para(rev("ins", "5", OTHER, run("Their words")))], [para(rev("ins", "5", OTHER, run("Their wordz")))],
        "existing_revisions_preserved"),
    "their_deletion_dropped": (
        [para(run("A"), rev("del", "5", OTHER, run("B", "delText")))], [para(run("A"))], "existing_revisions_preserved"),
    "fake_piece_of_their_insertion": (
        [para(rev("ins", "5", OTHER, run("Their words")))],
        [para(rev("ins", "5", OTHER, run("Their words")), rev("ins", "9", OTHER, run(" extra")))], "existing_revisions_preserved"),
    "their_table_revision_dropped": (
        [TABLE.format(rows=TRACKED_CELL_ROW)], [TABLE.format(rows=ROW)], "existing_revisions_preserved"),
}


@pytest.mark.parametrize("name", sorted(DEFECTS))
def test_defects_are_reported(tmp_path, name):
    original, candidate, expected = DEFECTS[name]
    failed, report = check(tmp_path, original, candidate)
    assert expected in failed, report
    assert report["status"] == "issues_found"
    assert expected in report["summary"]["defects"]
    assert "layout_and_legal_intent" in report["summary"]["not_checked"]


# Word 16.95 opens these and accepts or rejects them like the native forms: a warning, not a defect.
WARNINGS = {
    "insertion_inside_their_insertion": (
        [para(rev("ins", "5", OTHER, run("Right Holder")))],
        [para(rev("ins", "5", OTHER, rev("del", "6", ME, run("Right Holder", "delText")), rev("ins", "7", ME, run("Licensee"))))]),
    "deletion_inside_deletion": (
        [para(rev("del", "5", OTHER, run("Gone", "delText")))],
        [para(rev("del", "5", OTHER, rev("del", "6", ME, run("Gone", "delText"))))]),
}


@pytest.mark.parametrize("name", sorted(WARNINGS))
def test_placement_is_a_warning_not_a_defect(tmp_path, name):
    original, candidate = WARNINGS[name]
    failed, report = check(tmp_path, original, candidate)
    assert not failed, report
    assert report["status"] == "checked_with_limits"
    assert report["summary"]["warnings"] == ["new_revision_placement"]


def test_untracked_change_is_located(tmp_path):
    _, report = check(tmp_path, [para(run("Intro")), para(run("Pay within 10 days"))],
                      [para(run("Intro")), para(run("Pay within 70 days"))])
    detail = next(c["detail"] for c in report["checks"] if c["check"] == "untracked_changes")
    difference = detail["parts"][0]["differences"][0]
    assert difference["where"] == "paragraph 2" and difference["aspect"] == "text"
    assert (difference["original_text"], difference["text_after_undo"]) == ("Pay within 10 days", "Pay within 70 days")


COMMENT = (f'<w:comment w:id="0" w:author="{OTHER}" w:date="{D}">'
           '<w:p w14:paraId="1A2B3C4D"><w:r><w:t>Keep this</w:t></w:r></w:p></w:comment>')


def new_comment(author):
    return f'<w:comment w:id="1" w:author="{author}" w:date="{D}"><w:p><w:r><w:t>Mine</w:t></w:r></w:p></w:comment>'


def comments(*entries):
    return {"word/comments.xml": f"<w:comments {NS}>{''.join(entries)}</w:comments>"}


def resolved(flag):
    return {"word/commentsExtended.xml": f'<w15:commentsEx {NS}><w15:commentEx w15:paraId="1A2B3C4D" w15:done="{flag}"/></w15:commentsEx>'}


def anchored(cid, text):
    return para(f'<w:commentRangeStart w:id="{cid}"/>', run(text), f'<w:commentRangeEnd w:id="{cid}"/>',
                f'<w:r><w:commentReference w:id="{cid}"/></w:r>')


def test_new_anchored_comment_passes(tmp_path):
    failed, report = check(tmp_path, [anchored(0, "Theirs"), para(run("Mine here"))],
                           [anchored(0, "Theirs"), anchored(1, "Mine here")],
                           {**comments(COMMENT), **resolved("0")}, {**comments(COMMENT, new_comment(ME)), **resolved("0")})
    assert not failed, report


@pytest.mark.parametrize("change,expected", [
    ("comment_removed", "existing_comments_preserved"),
    ("anchors_removed", "untracked_changes"),
    ("resolved_flag_flipped", "existing_comments_preserved"),
    ("new_comment_unanchored", "new_comments"),
    ("new_comment_wrong_author", "new_comments"),
    ("comment_link_changed", "existing_comments_preserved"),
    ("duplicate_comment_id", "new_comments"),
])
def test_comment_defects(tmp_path, change, expected):
    original = [anchored(0, "Theirs"), para(run("Mine here"))]
    candidate, parts = list(original), {**comments(COMMENT), **resolved("0")}
    if change == "comment_removed":
        parts.update(comments())
    elif change == "anchors_removed":
        candidate = [para(run("Theirs")), para(run("Mine here"))]
    elif change == "resolved_flag_flipped":
        parts.update(resolved("1"))
    elif change == "new_comment_unanchored":
        parts.update(comments(COMMENT, new_comment(ME)))
    elif change == "comment_link_changed":  # the resolved flag no longer reaches this comment
        parts.update(comments(COMMENT.replace("1A2B3C4D", "7FA12345")))
    elif change == "duplicate_comment_id":  # placed first, so a dictionary of entries would keep the original
        parts.update(comments(COMMENT.replace("Keep this", "Different text"), COMMENT))
    else:
        candidate = [anchored(0, "Theirs"), anchored(1, "Mine here")]
        parts.update(comments(COMMENT, new_comment(OTHER)))
    failed, report = check(tmp_path, original, candidate, {**comments(COMMENT), **resolved("0")}, parts)
    assert expected in failed, report


def test_package_changes(tmp_path):
    body = [para(run("Text"))]
    header = f'<w:hdr {NS}><w:p><w:r><w:t>Header</w:t></w:r></w:p></w:hdr>'
    failed, _ = check(tmp_path, body, body, {"word/header1.xml": header}, {})
    assert {"untracked_changes", "package_parts"} <= failed
    failed, _ = check(tmp_path, body, body, {}, {"word/vbaProject.bin": b"x"})
    assert "package_parts" in failed

    def styles(extra=""):
        return f'<w:styles {NS}><w:style w:styleId="Normal"><w:rPr><w:sz w:val="22"/></w:rPr></w:style>{extra}</w:styles>'

    failed, report = check(tmp_path, body, body, {"word/styles.xml": styles()},
                           {"word/styles.xml": styles('<w:style w:styleId="Mine"/>')})
    assert not failed, report
    failed, _ = check(tmp_path, body, body, {"word/styles.xml": styles()},
                      {"word/styles.xml": styles().replace('w:val="22"', 'w:val="28"')})
    assert "package_parts" in failed
    repeated = styles().replace("<w:style ", '<w:style w:styleId="Normal"><w:rPr><w:sz w:val="28"/></w:rPr></w:style><w:style ', 1)
    failed, report = check(tmp_path, body, body, {"word/styles.xml": styles()}, {"word/styles.xml": repeated})
    assert "package_parts" in failed, report

    def data(inner):
        return {"customXml/item1.xml": f'<data xmlns="urn:test">{inner}</data>'}

    for before, after in [("<price>10000</price>", "<price>90000</price>"),
                          ("<price> 10000</price>", "<price>10000</price>"),
                          ("<label>Pay<em>within</em> 10 days</label>", "<label>Pay<em>within</em>10 days</label>"),
                          ("<label><em>Pay</em> <em>within</em></label>", "<label><em>Pay</em><em>within</em></label>")]:
        failed, report = check(tmp_path, body, body, data(before), data(after))
        assert "package_parts" in failed, (before, report)
    failed, report = check(tmp_path, body, body, data("<price>10000</price>"),
                           {"customXml/item1.xml": b'<?xml version="1.0" standalone="yes"?>\n'
                                                   b'<data xmlns="urn:test"><price>10000</price></data>'})
    assert not failed, report  # same data, only the XML declaration differs


def test_inherited_bad_markup_is_explicit_not_silently_accepted(tmp_path):
    body = [para(rev("del", "5", OTHER, run("Old")))]  # ordinary text inside an existing deletion
    failed, report = check(tmp_path, body, body)
    assert not failed, report
    assert {c["check"]: c["status"] for c in report["checks"]}["inherited_deletion_text"] == "not_checked"
