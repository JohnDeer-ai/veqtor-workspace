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
             RELS: relationships((COMMENTS, selected.upper()), (EXTENDED, "/WORD/" + extras.upper()))}
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


HEADER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header"


def test_link_type_and_word_root_determine_notes_and_header_roles(tmp_path):
    comment_part, header_part = "word/header-notes.xml", "word/notice-margin.xml"
    links = relationships((COMMENTS, "header-notes.xml"), (HEADER, "notice-margin.xml"))
    primary = comments(note(0, "Review body"))["word/comments.xml"]

    def document(name, header):
        return docx(tmp_path / name, [anchored(0, "Body")], {RELS: links, comment_part: primary,
                    header_part: f'<w:hdr {NS}>{para(run(header))}</w:hdr>'})

    original = document("original.docx", "Notice within 30 days")
    candidate = document("candidate.docx", "Notice within 45 days")
    inventory = read_docx(candidate, format="data")
    assert inventory["comments"] == 1
    assert [item["location"]["part"] for item in inventory["items"] if item["type"] == "paragraph"] == [
        "word/document.xml", header_part]
    report = check_docx(original, candidate, "Reviewer")
    changed, = report["changes"]["items"]
    assert changed["after"]["location"]["part"] == header_part
    assert changed["after"]["text_excerpt"] == "Notice within 45 days"


def corrupt_stored_member(path, name):
    import struct
    from zipfile import ZIP_STORED, ZipFile

    with ZipFile(path) as archive:
        member = archive.getinfo(name)
        assert member.compress_type == ZIP_STORED
    with path.open("r+b") as handle:
        handle.seek(member.header_offset + 26)
        name_length, extra_length = struct.unpack("<HH", handle.read(4))
        offset = member.header_offset + 30 + name_length + extra_length
        handle.seek(offset)
        damaged = bytes([handle.read(1)[0] ^ 1])
        handle.seek(offset)
        handle.write(damaged)


@pytest.mark.parametrize("failure", ["xml", "crc"])
def test_linked_unreadable_header_keeps_body_and_declares_partial_comparison(tmp_path, failure):
    header = "word/notice-margin.xml"
    links = relationships((HEADER, "notice-margin.xml"))
    valid_header = f'<w:hdr {NS}>{para(run("Notice"))}</w:hdr>'
    original = docx(tmp_path / "original.docx", [para(run("Pay in 30 days"))], {RELS: links, header: valid_header})
    candidate = docx(tmp_path / "candidate.docx", [para(run("Pay in 45 days"))],
                     {RELS: links, header: "<broken>" if failure == "xml" else valid_header})
    if failure == "crc":
        corrupt_stored_member(candidate, header)
    data = read_docx(candidate, format="data")
    assert header in data["unread_story_parts"]
    assert [item["text"] for item in data["items"]] == ["Pay in 45 days"]
    report = check_docx(original, candidate, "Reviewer")
    assert report["status"] == "diagnostic"
    assert [(issue["part"], issue["side"]) for issue in report["unchecked_story_xml"]] == [(header, "candidate")]
    assert "existing_revisions_preserved" in report["summary"]["not_checked"]
    assert "Unavailable story parts" in report["changes"]["scope"]
    changed, = report["changes"]["items"]
    assert changed["after"]["text_excerpt"] == "Pay in 45 days"


def test_bad_comment_relationship_does_not_exempt_changed_theme(tmp_path):
    theme = "word/theme/theme1.xml"
    before = {theme: '<theme><color val="110000"/></theme>'}
    after = {RELS: relationships((COMMENTS, "theme/theme1.xml")), theme: '<theme><color val="220000"/></theme>'}
    body = [para(run("Clause"))]
    original = docx(tmp_path / "original.docx", body, before)
    candidate = docx(tmp_path / "candidate.docx", body, after)
    report = check_docx(original, candidate, "Reviewer")
    assert report["comment_facts"]["candidate"]["total"] is None
    package = next(item for item in report["checks"] if item["check"] == "package_parts")
    assert any(issue.get("part") == theme for issue in package["detail"]["issues"])


def test_comment_fact_retains_anchor_boundaries_and_strike_in_compact_excerpt(tmp_path):
    body = [
        para('<w:commentRangeStart w:id="0"/>', '<w:r><w:t>Pay</w:t><w:tab/><w:t>now</w:t><w:br/><w:t>today</w:t></w:r>'),
        para(run("and tomorrow"), '<w:commentRangeEnd w:id="0"/>', '<w:r><w:commentReference w:id="0"/></w:r>'),
    ]
    plain_prefix = "a" * 234
    comment = ('<w:comment w:id="0" w:author="Reviewer">'
               + para(run("A"), run("B", rpr="<w:rPr><w:strike/></w:rPr>"))
               + '<w:p w14:paraId="000000AA">' + run(plain_prefix)
               + run("double", rpr="<w:rPr><w:dstrike/></w:rPr>") + "</w:p></w:comment>")
    parts = {**comments(comment), "word/commentsExtended.xml": extension('<w15:commentEx w15:paraId="000000AA"/>')}
    path = docx(tmp_path / "original.docx", body, parts)
    item, = [it for it in read_docx(path, format="data")["items"] if it["type"] == "comment"]
    assert item["resolved"] is False
    assert item["anchor_markup"] == "Pay\tnow\ntoday\nand tomorrow"
    assert item["strikethrough_spans"] == [
        {"start": 1, "end": 2, "kind": "strike"}, {"start": 237, "end": 243, "kind": "double_strike"}]
    assert "A[strike]B[/strike]" in read_docx(path)

    plain = docx(tmp_path / "plain.docx", body, {**parts, **comments(
        comment.replace("<w:strike/>", "").replace("<w:dstrike/>", ""))})
    facts = check_docx(path, plain, "Reviewer")["comment_facts"]
    compact = facts["original"]["items"][0]
    assert compact["text_truncated"] is True and compact["strikethrough_spans"][-1]["end"] == 240
    assert compact["formatted_excerpt"].endswith("[double-strike]dou[/double-strike]")
    assert "strikethrough_spans" not in facts["candidate"]["items"][0]


def test_excluding_reference_keeps_its_run_formatting_history(tmp_path):
    changed = ('<w:r><w:rPr><w:b/><w:rPrChange w:id="77" w:author="Reviewer" w:date="2026-01-01T00:00:00Z">'
               '<w:rPr/></w:rPrChange></w:rPr><w:commentReference w:id="0"/></w:r>')
    plain = '<w:r><w:rPr><w:b/></w:rPr><w:commentReference w:id="0"/></w:r>'
    parts = comments(note(0, "Review"))
    original = docx(tmp_path / "original.docx", [para(run("Clause"), changed)], parts)
    candidate = docx(tmp_path / "candidate.docx", [para(run("Clause"), plain)], parts)
    report = check_docx(original, candidate, "Reviewer")
    assert "existing_revisions_preserved" in report["summary"]["observations"]


@pytest.mark.parametrize("member", ["word/comments.xml", "word/document.xml", "[Content_Types].xml"])
def test_bad_crc_is_local_unless_a_required_member_is_unreadable(tmp_path, member):
    parts = comments(note(0, "Check"))
    original = docx(tmp_path / "original.docx", [anchored(0, "Pay in 30 days")], parts)
    candidate = docx(tmp_path / "candidate.docx", [anchored(0, "Pay in 45 days")], parts)
    corrupt_stored_member(candidate, member)
    report = check_docx(original, candidate, "Reviewer")
    if member == "word/comments.xml":
        data = read_docx(candidate, query="45 days", context=0, format="data")
        assert data["matched_paragraphs"] == 1 and data["comments"] is None
        assert report["status"] == "diagnostic" and report["comment_facts"]["candidate"]["total"] is None
        assert report["changes"]["items"][0]["after"]["text_excerpt"] == "Pay in 45 days"
        entry, = report["unchecked_auxiliary_xml"]
        assert (entry["part"], entry["stage"], entry["bytes_changed"]) == (member, "zip_reading", None)
    else:
        assert report["status"] == "error"
        assert member in report["checks"][0]["detail"]
        with pytest.raises(ValueError):
            read_docx(candidate)


def test_valid_relationships_do_not_read_an_orphaned_header(tmp_path):
    body = [para(run("Body text"))]
    header = "word/header1.xml"
    shared = {header: f'<w:hdr {NS}>{para(run("Previous header"))}</w:hdr>'}
    original = docx(tmp_path / "original.docx", body,
                    {**shared, RELS: relationships((HEADER, "header1.xml"))})
    candidate = docx(tmp_path / "candidate.docx", body, {**shared, RELS: relationships()})
    inventory = read_docx(candidate, query="Previous header", context=0, format="data")
    assert inventory["matched_paragraphs"] == 0 and inventory["paragraphs"] == 1
    report = check_docx(original, candidate, "Reviewer")
    change, = report["changes"]["items"]
    assert change["before"]["location"]["part"] == header and change["after"] is None
    assert "unchecked_story_xml" not in report


_REVISION_CHECKS = {"new_revision_authors", "new_revision_dates", "new_revision_ids", "new_revision_structure",
                    "new_revision_placement", "redline_quality"}


@pytest.mark.parametrize("mode", ["corrupt", "missing_id", "wrong_type", "deleted"])
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_section_reference_distinguishes_unavailable_and_removed_story(tmp_path, mode, reverse):
    header = "word/header1.xml"
    section = ('<w:sectPr xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
               '<w:headerReference w:type="default" r:id="rId1"/></w:sectPr>')
    shared = {header: f'<w:hdr {NS}>{para(run("Notice"))}</w:hdr>'}
    original = docx(tmp_path / "original.docx", [para(run("Pay in 30 days")), section],
                    {**shared, RELS: relationships((HEADER, "header1.xml"))})
    rels = {"corrupt": "<broken>", "missing_id": relationships(), "deleted": relationships(),
            "wrong_type": relationships((COMMENTS, "header1.xml"))}[mode]
    body = [para(run("Pay in 45 days"))]
    if mode != "deleted":
        body.append(section)
    candidate = docx(tmp_path / "candidate.docx", body, {**shared, RELS: rels})
    read = read_docx(candidate, format="data")
    assert read["paragraphs"] == 1
    if mode in ("missing_id", "wrong_type"):
        assert "rId1" in read["unread_story_parts"][RELS]
    if reverse:
        original, candidate = candidate, original
    report = check_docx(original, candidate, "Reviewer")
    changes = report["changes"]["items"]
    if mode == "deleted":
        assert len(changes) == 2 and "unchecked_story_xml" not in report
        assert any(item["kind"] == ("insert" if reverse else "delete") for item in changes)
    else:
        assert len(changes) == 1 and changes[0]["kind"] == "replace"
        issue, = report["unchecked_story_xml"]
        assert issue["part"] == RELS and issue["side"] == ("original" if reverse else "candidate")
        assert _REVISION_CHECKS <= set(report["summary"]["not_checked"])


def test_incomplete_story_does_not_hide_revision_findings_in_readable_body(tmp_path):
    header = "word/header1.xml"
    shared = {RELS: relationships((HEADER, "header1.xml")), header: f'<w:hdr {NS}>{para(run("Notice"))}</w:hdr>'}
    original = docx(tmp_path / "original.docx", [para(run("Clause"))], shared)
    new_revision = '<w:ins w:author="Another reviewer" w:id="7" w:date="2026-01-01T00:00:00Z">' + run(" added") + '</w:ins>'
    candidate = docx(tmp_path / "candidate.docx", [para(run("Clause"), new_revision)],
                     {**shared, header: "<broken>"})
    report = check_docx(original, candidate, "Reviewer")
    checks = {item["check"]: item for item in report["checks"]}
    assert checks["new_revision_authors"]["status"] == "observation"
    assert checks["new_revision_authors"]["detail"]["new_revisions_in_readable_stories"] == 1
    assert checks["new_revision_authors"]["detail"]["unexpected_authors"][0]["author"] == "Another reviewer"
    assert checks["new_revision_authors"]["detail"]["unread_story_parts"] == [header]
    assert all(checks[name]["status"] == "not_checked" for name in _REVISION_CHECKS - {"new_revision_authors"})


def test_partial_revision_checks_keep_duplicate_ids_without_speculative_repair_warning(tmp_path):
    header = "word/header1.xml"
    links = relationships((HEADER, "header1.xml"))
    original = docx(tmp_path / "original.docx", [para(run("Clause"))], {RELS: links, header: "<broken>"})
    addition = '<w:ins w:author="Reviewer" w:id="7" w:date="2026-01-01T00:00:00Z">' + run(" added") + '</w:ins>'
    candidate = docx(tmp_path / "candidate.docx", [para(run("Clause"), addition, addition)],
                     {RELS: links, header: f'<w:hdr {NS}>{para(run("Notice"))}</w:hdr>'})
    report = check_docx(original, candidate, "Reviewer")
    assert report["status"] == "diagnostic"
    checks = {item["check"]: item for item in report["checks"]}
    ids = checks["new_revision_ids"]
    assert ids["status"] == "observation"
    assert ids["detail"]["issues"][0]["id"] == "7"
    assert ids["detail"]["unread_story_parts"] == [header]
    structure = checks["new_revision_structure"]
    assert structure["status"] == "not_checked"
    assert structure["detail"]["unread_story_parts"] == [header]
    assert "note" not in structure["detail"]


@pytest.mark.parametrize("changed_text", [False, True], ids=["case_only", "changed_text"])
def test_case_only_archive_names_keep_parts_aligned_and_locators_literal(tmp_path, changed_text):
    links = relationships((COMMENTS, "notes.xml"), (HEADER, "notice.xml"))
    primary = comments(note(0, "Question"))["word/comments.xml"]

    def document(name, note_part, header_part, text):
        return docx(tmp_path / name, [anchored(0, "Body")], {RELS: links, note_part: primary,
                    header_part: f'<w:hdr {NS}>{para(run(text))}</w:hdr>'})

    original = document("original.docx", "word/notes.xml", "word/notice.xml", "Notice 30 days")
    candidate = document("candidate.docx", "Word/Notes.XML", "Word/Notice.XML",
                         "Notice 45 days" if changed_text else "Notice 30 days")
    report = check_docx(original, candidate, "Reviewer")
    assert report["comment_facts"]["original"]["total"] == report["comment_facts"]["candidate"]["total"] == 1
    assert report["changed_parts"] == (["Word/Notice.XML"] if changed_text else [])
    checks = {item["check"]: item for item in report["checks"]}
    assert checks["package_parts"]["status"] == "passed"
    assert report["changes"]["total"] == int(changed_text)
    if changed_text:
        change, = report["changes"]["items"]
        assert change["kind"] == "replace"
        assert change["before"]["location"]["part"] == "word/notice.xml"
        assert change["after"]["location"]["part"] == "Word/Notice.XML"
        assert change["after"]["text_excerpt"] == "Notice 45 days"


def test_case_ambiguous_comment_target_is_never_chosen(tmp_path):
    primary = comments(note(0, "First"))["word/comments.xml"]
    path = docx(tmp_path / "ambiguous.docx", [anchored(0, "Clause")],
                {RELS: relationships((COMMENTS, "notes%20archive.xml")),
                 "word/Notes%20Archive.xml": primary, "word/notes%20archive.xml": primary})
    inventory = read_docx(path, format="data")
    assert inventory["comments"] is None
    assert "more than one archive member" in inventory["comment_issues"][0]["issue"].lower()


@pytest.mark.parametrize("wrong_root", [False, True])
def test_first_modern_comment_parts_require_matching_role_and_root(tmp_path, wrong_root):
    original = docx(tmp_path / "original.docx", [para(run("Clause"))])
    ns_ids = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
    ns_extensible = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
    ids = "word/ids%20registry.xml"
    parts = {**comments(note(0, "Selected comment")),
             ids: "<theme/>" if wrong_root else f'<ci:commentsIds xmlns:ci="{ns_ids}"/>',
             "word/ext-list.xml": f'<ce:commentsExtensible xmlns:ce="{ns_extensible}"/>',
             "word/team.xml": f'<w15:people {NS}/>',
             RELS: relationships(
                 (COMMENTS, "comments.xml"),
                 ("http://schemas.microsoft.com/office/2016/09/relationships/commentsIds", "IDS%20REGISTRY.XML"),
                 ("http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible", "ext-list.xml"),
                 ("http://schemas.microsoft.com/office/2011/relationships/people", "team.xml"))}
    candidate = docx(tmp_path / "candidate.docx", [anchored(0, "Clause")], parts)
    report = check_docx(original, candidate, "Reviewer")
    assert report["comment_facts"]["net_change"] == 1
    package = next(item for item in report["checks"] if item["check"] == "package_parts")
    unexpected = [issue for issue in package.get("detail", {}).get("issues", []) if issue["issue"] == "unexpected new parts"]
    assert unexpected == ([{"issue": "unexpected new parts", "parts": [ids]}] if wrong_root else [])


def test_comment_selected_fallback_tokens_match_the_paragraph_projection(tmp_path):
    def fallback(tag, unexpected):
        return ('<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
                'xmlns:qa="urn:test:optional"><mc:Choice Requires="qa">'
                f'<w:t>{unexpected}</w:t></mc:Choice><mc:Fallback><w:{tag}/></mc:Fallback></mc:AlternateContent>')

    body = [para('<w:commentRangeStart w:id="0"/>',
                 '<w:r><w:t>Pay</w:t>' + fallback("tab", "discarded") + '<w:t>now</w:t>'
                 + fallback("br", "duplicate") + '<w:t>today</w:t></w:r>',
                 '<w:commentRangeEnd w:id="0"/>', '<w:r><w:commentReference w:id="0"/></w:r>')]
    path = docx(tmp_path / "fallback.docx", body, comments(note(0, "Note")))
    paragraph, selected = read_docx(path, format="data")["items"]
    assert paragraph["text"] == selected["anchor_markup"] == "Pay\tnow\ntoday"
