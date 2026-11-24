# SPDX-License-Identifier: Apache-2.0
"""Per-file DOCX comment facts and limited internal relationship resolution."""

import pytest
from test_checks import D, NS, OTHER, anchored, comments, docx, para, run

from veqtor_workspace.checks import check_docx
from veqtor_workspace.reading import read_docx

RELS = "word/_rels/document.xml.rels"
COMMENTS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
EXTENDED = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"


def note(cid, text, *, para_id=None, author=OTHER):
    attrs = f' w14:paraId="{para_id}"' if para_id else ""
    return f'<w:comment w:id="{cid}" w:author="{author}" w:date="{D}"><w:p{attrs}>{run(text)}</w:p></w:comment>'


def extension(*entries):
    return f'<w15:commentsEx {NS}>{"".join(entries)}</w15:commentsEx>'


def relationships(*links):
    labels = "".join(f'<Relationship Type="{kind}" Target="{target}" Id="rId{i}"/>'
                     for i, (kind, target) in enumerate(links, 1))
    return f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{labels}</Relationships>'


def test_literal_encoded_parts_win_over_stale_and_decoded_copies(tmp_path):
    selected, extras = "review%20notes.xml", "review%20status.xml"
    primary = comments(note(0, "Question", para_id="000000AA"),
                       note(9, "Useful answer", para_id="000000BB"))["word/comments.xml"]
    metadata = extension('<w15:commentEx w15:paraId="000000aa" w15:done="1"/>',
                         '<w15:commentEx w15:paraId="000000bb" w15:paraIdParent="000000aa" w15:done="0"/>')
    parts = {**comments(note(0, "Stale note")),
             "word/commentsExtended.xml": extension('<w15:commentEx w15:paraId="000000AA" w15:done="0"/>'),
             "word/" + selected: primary, "word/" + extras: metadata,
             "word/review notes.xml": comments(note(0, "Wrong decoded part"))["word/comments.xml"],
             "word/review status.xml": extension(),
             RELS: relationships((COMMENTS, selected), (EXTENDED, "/" + "word/" + extras))}
    path = docx(tmp_path / "linked.docx", [anchored(0, "Selected phrase")], parts)
    data = read_docx(path, query="Useful answer", context=0, format="data")
    assert data["comments"] == 2 and data["matched_paragraphs"] == 1
    parent, reply = [item for item in data["items"] if item["type"] == "comment"]
    assert parent["text"] == "Question" and parent["resolved"] is True
    assert reply["parent_comment_id"] == "0" and reply["resolved"] is False
    assert reply["anchor_markup"] == "Selected phrase" and reply["paragraphs"] == ["word/document.xml#p1"]

    report = check_docx(path, path, "Reviewer")
    facts = report["comment_facts"]
    assert facts["net_change"] == 0
    assert facts["original"] == facts["candidate"] and facts["original"]["total"] == 2
    assert facts["original"]["items"][1]["text"] == "Useful answer"


@pytest.mark.parametrize("failure", ["broken_thread", "missing_thread", "ambiguous_thread", "missing_comments",
                                    "broken_comments", "external_comments", "dangling_comments", "broken_relationships"])
def test_unavailable_comment_fields_leave_table_and_body_comparison_readable(tmp_path, failure):
    def body(text):
        return [f'<w:tbl><w:tr><w:tc>{anchored(0, text)}</w:tc><w:tc>{para(run("Second cell"))}</w:tc></w:tr></w:tbl>']

    primary = comments(note(0, "Question", para_id="000000AA"))["word/comments.xml"]
    metadata = extension('<w15:commentEx w15:paraId="000000AA" w15:done="1"/>')
    links = [(COMMENTS, "notes.xml"), (EXTENDED, "thread.xml")]
    parts = {RELS: relationships(*links), "word/notes.xml": primary, "word/thread.xml": metadata}
    original = docx(tmp_path / "original.docx", body("Initial clause"), parts)
    if failure == "broken_thread":
        parts["word/thread.xml"] = "<broken>"
    elif failure == "missing_thread":
        del parts["word/thread.xml"]
    elif failure == "ambiguous_thread":
        parts[RELS] = relationships(*links, (EXTENDED, "commentsExtended.xml"))
    elif failure == "missing_comments":
        del parts["word/notes.xml"]
    elif failure == "broken_comments":
        parts["word/notes.xml"] = "<broken>"
    elif failure == "external_comments":
        parts[RELS] = relationships((COMMENTS, "https://example.test/private.xml"), (EXTENDED, "thread.xml"))
    elif failure == "dangling_comments":
        del parts[RELS]
    else:
        parts[RELS] = "<broken>"
    # Neither legacy copy is authoritative while the document has explicit links.
    if failure != "dangling_comments":
        parts.update(comments(note(0, "Stale copy")))
        parts["word/commentsExtended.xml"] = metadata

    candidate = docx(tmp_path / "candidate.docx", body("Updated clause"), parts)
    inventory = read_docx(candidate, query="Updated clause", context=0, format="data")
    assert inventory["matched_paragraphs"] == 1 and inventory["comment_issues"]
    row = read_docx(candidate, table_row="p1")
    assert "Updated clause" in row and "Second cell" in row and "Comment facts limited" in row
    if failure.endswith("_thread"):
        comment, = [item for item in inventory["items"] if item["type"] == "comment"]
        assert inventory["comments"] == 1 and comment["text"] == "Question"
        assert comment["resolved"] is None and comment["parent_status"] == "unknown"
    else:
        assert inventory["comments"] is None
        assert not any(item["type"] == "comment" for item in inventory["items"])

    report = check_docx(original, candidate, "Reviewer")
    assert report["status"] == "diagnostic" and report["summary"]["errors"] == []
    assert report["changes"]["items"][0]["after"]["text_excerpt"] == "Updated clause"
    facts = report["comment_facts"]
    assert facts["original"]["total"] == 1
    assert facts["candidate"]["total"] == inventory["comments"]
    assert facts["net_change"] == (0 if failure.endswith("_thread") else None)
    assert facts["candidate"]["issues"]
    assert "comment_facts" in report["summary"]["not_checked"]


def test_comment_counts_and_body_differences_survive_auxiliary_canonicalization_failure(tmp_path):
    part = "customXml/item1.xml"
    original = docx(tmp_path / "original.docx", [anchored(0, "Original clause")],
                    {**comments(note(0, "Original note"), note(1, "Second note")),
                     part: '<data xmlns="urn:valid">Old</data>'})
    candidate = docx(tmp_path / "candidate.docx", [anchored(0, "Updated clause")],
                     {**comments(*(note(i, f"Comment {i}") for i in range(149))),
                      part: '<data xmlns="relative">New</data>', "docProps/custom.xml": "<broken>"})
    report = check_docx(original, candidate, "Reviewer")
    assert report["status"] == "diagnostic"
    facts = report["comment_facts"]
    assert (facts["original"]["total"], facts["candidate"]["total"], facts["net_change"]) == (2, 149, 147)
    assert facts["candidate"]["remaining_items"] == 144
    assert [item["text"] for item in facts["original"]["items"]] == ["Original note", "Second note"]
    assert len([item for item in read_docx(candidate, format="data")["items"] if item["type"] == "comment"]) == 149
    assert report["changes"]["items"][0]["after"]["text_excerpt"] == "Updated clause"
    assert {(entry["part"], entry["stage"], entry["bytes_changed"]) for entry in report["unchecked_auxiliary_xml"]} == {
        (part, "canonicalization", True), ("docProps/custom.xml", "parsing", True)}
    assert {part, "word/comments.xml", "word/document.xml"} <= set(report["changed_parts"])
    assert {"new_comments", "existing_comments_preserved", "new_comment_dates"}.isdisjoint(
        entry["check"] for entry in report["checks"])


def test_malformed_main_document_is_not_downgraded_to_an_auxiliary_issue(tmp_path):
    original = docx(tmp_path / "original.docx", [para(run("Clause"))])
    candidate = docx(tmp_path / "candidate.docx", [],
                     {"word/document.xml": "<broken>", "word/commentsExtended.xml": "<also-broken>"})
    report = check_docx(original, candidate, "Reviewer")
    assert report["status"] == "error"
    assert "word/document.xml" in report["checks"][0]["detail"]
    assert "changes" not in report
    with pytest.raises(ValueError, match="word/document.xml"):
        read_docx(candidate)


def test_relocated_unused_comment_ids_can_fail_without_hiding_body_or_comments(tmp_path):
    ids = "word/review-ids.xml"
    links = relationships((COMMENTS, "comments.xml"),
                          ("http://schemas.microsoft.com/office/2016/09/relationships/commentsIds", "review-ids.xml"))
    shared = {**comments(note(0, "Check payment"), note(1, "Check notice")), RELS: links}
    original = docx(tmp_path / "original.docx", [anchored(0, "Pay within 30 days"), anchored(1, "Notice")],
                    {**shared, ids: '<ci:commentsIds xmlns:ci="http://schemas.microsoft.com/office/word/2016/wordml/cid"/>'})
    candidate = docx(tmp_path / "candidate.docx", [anchored(0, "Pay within 45 days"), anchored(1, "Notice")],
                     {**shared, ids: "<broken>"})
    data = read_docx(candidate, format="data")
    assert data["comments"] == 2 and ids in data["unread_auxiliary_xml"]
    assert data["items"][0]["text"] == "Pay within 45 days"
    report = check_docx(original, candidate, "Reviewer")
    assert report["status"] == "diagnostic" and report["summary"]["errors"] == []
    facts = report["comment_facts"]
    assert facts["original"]["total"] == facts["candidate"]["total"] == 2
    assert facts["candidate"]["issues"] == []
    entry, = report["unchecked_auxiliary_xml"]
    assert (entry["side"], entry["part"], entry["stage"]) == ("candidate", ids, "parsing")
    changed, = report["changes"]["items"]
    assert (changed["before"]["text_excerpt"], changed["after"]["text_excerpt"]) == (
        "Pay within 30 days", "Pay within 45 days")
