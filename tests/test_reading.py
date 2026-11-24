# SPDX-License-Identifier: Apache-2.0
"""Read evidence, not a reconstructed negotiation history or a Word layout."""

import json
from collections import Counter

import pytest
from test_checks import NS, check, comments, docx, mark, math, para, rev, run

from veqtor_workspace.checks import check_docx
from veqtor_workspace.reading import read_docx as read_document


def read_docx(path, **options):
    return read_document(path, format="data", **options)


def read_all(path, **options):
    return read_docx(path, **options)["items"]


def test_compact_reader_keeps_complete_large_unicode_and_unbounded_neighborhood(tmp_path):
    texts = [f"Пункт {i}: " + "договор и условия " * 75 for i in range(50)]
    texts[25] += " FIND_ME"
    path = docx(tmp_path / "whole.docx", [para(run(t)) for t in texts])
    output = read_document(path, query="FIND_ME", context=100)
    assert isinstance(output, str)
    for i, text in enumerate(texts, 1):
        assert f"p{i} {text}" in output
    assert output.index(texts[25]) < output.index(texts[0])
    assert len(output.encode()) > 24_000
    data = read_docx(path, query="FIND_ME", context=100)
    assert len(data["items"]) == 50 and "next_cursor" not in data
    assert sum(len(x["text"]) for x in data["items"]) == sum(map(len, texts))


def test_compact_reader_groups_warnings_and_keeps_comment_revision_provenance(tmp_path):
    paragraphs = [para('<w:fldSimple w:instr="PAGE">' + run('1') + '</w:fldSimple>',
                       rev('ins', str(i + 10), 'Earlier Author', run(f' clause {i}')),
                       '<w:commentRangeStart w:id="0"/>' if i == 0 else '', run(' anchored'),
                       '<w:commentRangeEnd w:id="0"/>' if i == 0 else '') for i in range(40)]
    note = '<w:comment w:id="0" w:author="Reviewer" w:date="2025-01-02T03:04:05Z">' + para(run('Keep this comment')) + '</w:comment>'
    path = docx(tmp_path / 'evidence.docx', paragraphs, comments(note))
    data = read_docx(path, details=True)
    text = read_document(path, details=True)
    warnings = {w for x in data['items'] if x['type'] == 'paragraph' for w in x['warnings']}
    assert warnings
    for warning in warnings:
        assert text.count(warning) == 1
    assert 'p1-p40' in text
    assert text.count('Earlier Author') == 40
    assert 'Reviewer' in text and '2025-01-02T03:04:05Z' in text and 'Keep this comment' in text
    assert 'anchor excerpt:  anchored' in text


def test_compact_reader_signals_retained_revisions_without_expanding_history(tmp_path):
    path = docx(tmp_path / 'pending.docx', [
        para(run('Unchanged')),
        para(run('Payment '), rev('del', '1', 'Earlier Author', run('30', 'delText')),
             rev('ins', '2', 'Later Author', run('45')), run(' days')),
    ])
    text = read_document(path)
    assert 'p1 Unchanged' in text
    assert 'p2 [r2] Payment 45 days' in text
    assert 'details=True' in text
    assert 'Earlier Author' not in text and 'Later Author' not in text
    details = read_document(path, details=True)
    assert 'p2 [r2] Payment 45 days' in details
    assert 'Earlier Author' in details and 'Later Author' in details
    assert [x['revision_count'] for x in read_all(path)] == [0, 2]


@pytest.mark.parametrize('options', [{'context': -1}, {'context': True}, {'format': 'unknown'}])
def test_invalid_reader_options_are_explicit(tmp_path, options):
    path = docx(tmp_path / 'source.docx', [para(run('Source'))])
    with pytest.raises(ValueError):
        read_document(path, **options)


@pytest.mark.parametrize("match_in", ["text", "revision", "comment"])
def test_search_starts_with_match_before_neighbor_history_and_keeps_all_items(tmp_path, match_in):
    previous = para(*(rev("ins", str(i), "Earlier author", run(f"Earlier history {i}. ")) for i in range(8)),
                    '<w:commentRangeStart w:id="0"/>', run("Neighbor"), '<w:commentRangeEnd w:id="0"/>')
    selected = para(run("Selected clause" + (" needle" if match_in == "text" else "")),
                    rev("del", "20", "Another author", run("needle", "delText")) if match_in == "revision" else "",
                    '<w:commentRangeStart w:id="1"/>', run(" retained text"), '<w:commentRangeEnd w:id="1"/>')
    parts = comments('<w:comment w:id="0" w:author="A">' + para(run("Neighbor comment")) + '</w:comment>',
                     '<w:comment w:id="1" w:author="B">'
                     + para(run("needle" if match_in == "comment" else "Selected comment")) + '</w:comment>')
    path = docx(tmp_path / "search.docx", [previous, selected, para(run("Following context"))], parts)
    first = read_docx(path, query="needle", details=True)
    assert first["items"][0]["type"] == "paragraph"
    assert first["items"][0]["text"].startswith("Selected clause")
    assert first["order"] == "matches_then_context"
    searched = read_all(path, query="needle", details=True)
    sequential = read_all(path, details=True)
    assert Counter(json.dumps(item, sort_keys=True) for item in searched) == Counter(
        json.dumps(item, sort_keys=True) for item in sequential)


def test_multiple_search_matches_precede_overlapping_context(tmp_path):
    text = ["Before", "needle first", "Between", "needle second", "After"]
    path = docx(tmp_path / "multiple.docx", [para(run(t)) for t in text])
    searched = read_all(path, query="needle")
    assert [item["text"] for item in searched] == [text[1], text[3], text[0], text[2], text[4]]
    assert [item["text"] for item in read_all(path)] == text
    assert read_docx(path)["order"] == "document"


def test_ten_authors_nested_edits_and_retained_history(tmp_path):
    fragments = [rev("ins", str(i), f"Author {i}", run(f"word{i} "),
                     rev("del", str(20 + i), f"Author {(i+1)%10}", run("removed ", "delText"))) for i in range(10)]
    path = docx(tmp_path / "history.docx", [para(*fragments)])
    items = read_all(path, details=True)
    assert "".join(x["text"] for x in items if x["type"] == "paragraph") == "".join(f"word{i} " for i in range(10))
    revisions = [x for x in items if x["type"] == "revision"]
    assert len(revisions) == 20
    assert len({x["author"] for x in revisions}) == 10
    deletion = next(x for x in revisions if x["id"] == "20")
    assert deletion["text"] == "removed " and deletion["parents"][0]["id"] == "0"
    assert read_docx(path, query="removed", context=0)["matched_paragraphs"] == 1


def test_long_unicode_is_complete_and_a_new_read_sees_latest_saved_text(tmp_path):
    text = "Очень длинное условие договора. " * 500
    path = docx(tmp_path / "long.docx", [para(run(text))])
    first = read_docx(path)
    assert first["items"][0]["text"] == text
    pieces = [x for x in read_all(path) if x["type"] == "paragraph"]
    assert "".join(x["text"] for x in pieces) == text
    assert len(pieces) == 1
    assert text in read_document(path)
    docx(path, [para(run(text + "changed"))])
    assert read_docx(path)["items"][0]["text"] == text + "changed"
    assert read_docx(path)["sha256"] != first["sha256"]


def test_comment_range_reply_and_unlocated_comment(tmp_path):
    c0 = '<w:comment w:id="0" w:author="A"><w:p w14:paraId="AA"><w:r><w:t>Parent question</w:t></w:r></w:p></w:comment>'
    c1 = '<w:comment w:id="1" w:author="B"><w:p w14:paraId="BB"><w:r><w:t>Reply answer</w:t></w:r></w:p></w:comment>'
    c2 = '<w:comment w:id="2" w:author="C"><w:p><w:r><w:t>Unlocated note</w:t></w:r></w:p></w:comment>'
    parts = comments(c0, c1, c2)
    parts['word/commentsExtended.xml'] = (f'<w15:commentsEx {NS}>'
        '<w15:commentEx w15:paraId="AA" w15:done="1"/>'
        '<w15:commentEx w15:paraId="BB" w15:paraIdParent="AA" w15:done="0"/></w15:commentsEx>')
    body = [para(run("Outside "), '<w:commentRangeStart w:id="0"/>', run("first")),
            para(run("second"), '<w:commentRangeEnd w:id="0"/>', '<w:r><w:commentReference w:id="0"/></w:r>')]
    path = docx(tmp_path / "comments.docx", body, parts)
    found = [x for x in read_all(path) if x['type'] == 'comment']
    assert len(found) == 3
    assert found[0]['anchor_markup_excerpt'] == 'first\nsecond'
    assert len(found[0]['paragraphs']) == 2 and found[0]['resolved'] is True
    assert found[1]['parent_comment_id'] == '0' and found[1]['paragraphs'] == found[0]['paragraphs']
    assert found[2]['anchor_status'] == 'not_located'
    result = read_docx(path, query="Reply answer", context=0)
    assert result['matched_paragraphs'] == 2


def test_table_context_notes_fields_and_structural_limits(tmp_path):
    body = ['<w:tbl><w:tr><w:tc>' + para(run('Label')) + '</w:tc><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>'
            + para(run('Value'), '<w:r><w:tab/><w:t>cached field</w:t><w:fldChar w:fldCharType="end"/></w:r>')
            + '</w:tc></w:tr><w:tr><w:trPr>' + rev('del', '7', 'Other') + '</w:trPr><w:tc>'
            + para(run('deleted row')) + '</w:tc></w:tr></w:tbl>',
            para(run('Boundary'), ppr=mark('del', '8')),
            '<w:altChunk/>']
    parts = {'word/footnotes.xml': f'<w:footnotes {NS}><w:footnote w:id="4">{para(run("Footnote text"))}</w:footnote></w:footnotes>',
             'word/header1.xml': f'<w:hdr {NS}>{para(run("Header text"))}</w:hdr>'}
    path = docx(tmp_path / 'structure.docx', body, parts)
    found = read_all(path, details=True)
    value = next(x for x in found if x['type'] == 'paragraph' and x['text'].startswith('Value'))
    assert value['text'] == 'Value\tcached field'
    assert value['location']['cell'] == 2
    assert value['location']['neighbor_cell_excerpts'][0]['text'] == 'Label'
    assert len(value['warnings']) == 2
    assert any(x['type'] == 'revision' and x['id'] == '7' for x in found)
    assert not any(x['type'] == 'paragraph' and 'deleted row' in x['text'] for x in found)
    assert any('paragraph boundary' in warning for x in found for warning in x.get('warnings', []))
    assert any(x.get('location', {}).get('note_id') == '4' for x in found)
    assert read_docx(path)['unread_embedded_parts'] == ['word/document.xml']


@pytest.mark.parametrize('history_author', ['Ilya', 'Other'])
def test_split_earlier_revision_with_new_round_and_real_loss(tmp_path, history_author):
    old = [para(rev('ins', '5', history_author, run('the Right Holder shall pay')))]
    new = [para(rev('ins', '5', history_author, run('the ')),
                rev('ins', '8', history_author, rev('del', '6', 'Ilya', run('Right Holder', 'delText'), date='2026-02-01T00:00:00Z')),
                rev('ins', '7', 'Ilya', run('Licensee'), date='2026-02-01T00:00:00Z'),
                rev('ins', '9', history_author, run(' shall pay')))]
    failed, result = check(tmp_path, old, new)
    assert not failed and not result['summary']['warnings']
    assert result['changes']['total'] == 1
    assert result['changes']['items'][0]['after']['text_excerpt'] == 'the Licensee shall pay'
    damaged = [new[0].replace(' shall pay', ' shall')]
    failed, _ = check(tmp_path, old, damaged)
    assert 'existing_revisions_preserved' in failed


def test_same_author_timestamp_collision_reports_unknown_provenance(tmp_path):
    old = [para(rev('ins', '1', 'Ilya', run('Existing')))]
    new = [para(rev('ins', '1', 'Ilya', run('Existing')), rev('ins', '2', 'Ilya', run(' forged')))]
    failed, report = check(tmp_path, old, new)
    assert not failed
    assert 'revision_origins' in report['summary']['not_checked']


@pytest.mark.parametrize('ids', [(None, '3'), ('3', '3'), ('1', '3')])
@pytest.mark.parametrize('author', ['Ilya', 'Other'])
def test_split_revision_ids_checked_independently_of_origin(tmp_path, ids, author):
    old = [para(rev('ins', '1', author, run('ABC')))]
    pieces = [rev('ins', '1', author, run('A'))]
    for rid, text in zip(ids, 'BC'):
        piece = rev('ins', rid or 'missing', author, run(text))
        pieces.append(piece.replace(' w:id="missing"', '') if rid is None else piece)
    failed, _ = check(tmp_path, old, [para(*pieces)])
    assert 'new_revision_ids' in failed


def test_mixed_same_stamp_insertions_require_exact_restoration(tmp_path):
    old = [para(rev('ins', '1', 'Ilya', run('A B'))), para(run('Untouched'))]
    mixed = para(rev('ins', '1', 'Ilya', run('A ')), rev('ins', '2', 'Ilya', run('X ')),
                 rev('ins', '3', 'Ilya', run('B')))
    failed, report = check(tmp_path, old, [mixed, old[1]])
    assert not failed
    assert report['summary']['not_checked'] == ['revision_origins', 'layout_and_legal_intent']
    origins = next(c['detail'] for c in report['checks'] if c['check'] == 'revision_origins')
    assert origins[0]['ids'] == ['2']
    failed, report = check(tmp_path, old, [mixed, para(run('Unexpected change'))])
    assert 'untracked_changes' in failed
    # A missing part of the original is still a defect, even with identical timestamps.
    failed, _ = check(tmp_path, old, [mixed.replace('>B<', '>C<'), old[1]])
    assert 'existing_revisions_preserved' in failed
    # Uncertainty about text origins must not excuse lost formatting history in the same paragraph.
    properties = '<w:rPr>' + rev('rPrChange', '8', 'Other', '<w:rPr><w:b/></w:rPr>') + '</w:rPr>'
    formatted = old[0].replace('<w:p>', '<w:p>' + run('Prefix ', rpr=properties), 1)
    lost_formatting = mixed.replace('<w:p>', '<w:p>' + run('Prefix '), 1)
    failed, _ = check(tmp_path, [formatted], [lost_formatting])
    assert 'existing_revisions_preserved' in failed


@pytest.mark.parametrize('damage', ['deletion', 'author', 'date', 'paragraph_mark', 'formula', 'anchor', 'plain_formatting'])
def test_ambiguous_insertions_never_hide_other_same_paragraph_damage(tmp_path, damage):
    original = rev('ins', '1', 'Ilya', run('A B'))
    mixed = rev('ins', '1', 'Ilya', run('A ')) + rev('ins', '2', 'Ilya', run('X ')) + rev('ins', '3', 'Ilya', run('B'))
    deleted = rev('del', '9', 'Other', run(' Z', 'delText'))
    if damage in ('deletion', 'author', 'date'):
        old = para(original, deleted)
        changed = {'deletion': run(' Z'), 'author': deleted.replace('Other', 'Another reviewer'),
                   'date': deleted.replace('2026-01-01', '2025-12-20')}[damage]
        new = para(mixed, changed)
    elif damage == 'paragraph_mark':
        old, new = para(original, ppr=mark('del', '9', 'Other')), para(mixed)
    elif damage == 'formula':
        old, new = para(original, math('price = 100')), para(mixed)
    elif damage == 'anchor':
        old, new = para('<w:commentRangeStart w:id="0"/>', original, '<w:commentRangeEnd w:id="0"/>'), para(mixed)
    else:
        old, new = para(original, run('Kept', rpr='<w:rPr><w:b/></w:rPr>')), para(mixed, run('Kept'))
    failed, report = check(tmp_path, [old], [new])
    if damage == 'anchor':
        assert 'untracked_changes' not in failed
        assert report['changes']['total'] == 1
    else:
        assert 'untracked_changes' in failed
    if damage in ('deletion', 'author', 'date', 'paragraph_mark'):
        assert 'existing_revisions_preserved' in failed
    assert 'revision_origins' in report['summary']['not_checked']


def test_several_same_stamp_insertions_are_verified_together(tmp_path):
    old = [para(rev('ins', str(i), 'Ilya', run('A B'))) for i in (1, 4)]
    new = [para(rev('ins', str(i), 'Ilya', run('A ')), rev('ins', str(i+1), 'Ilya', run('X ')),
                rev('ins', str(i+2), 'Ilya', run('B'))) for i in (1, 4)]
    failed, report = check(tmp_path, old, new)
    assert not failed
    assert report['summary']['not_checked'] == ['revision_origins', 'layout_and_legal_intent']


@pytest.mark.parametrize('ids', [('5', '6'), ('6', '7')])
def test_split_formatting_keeps_history_with_fresh_ids(tmp_path, ids):
    def props(rid):
        return '<w:rPr><w:b/>' + rev('rPrChange', rid, 'Other', '<w:rPr/>') + '</w:rPr>'
    old = [para(run('AB', rpr=props('5')))]
    new = [para(run('A', rpr=props(ids[0])), run('B', rpr=props(ids[1])))]
    failed, report = check(tmp_path, old, new)
    assert not failed and not report['summary']['warnings']
    # Keeping one copy of a record must not hide its loss from another text span.
    failed, _ = check(tmp_path, old, [para(run('A', rpr=props('5')), run('B', rpr='<w:rPr><w:b/></w:rPr>'))])
    assert 'existing_revisions_preserved' in failed
    for changed in [props('6').replace('Other', 'Changed author'), props('6').replace('2026-01-01', '2026-02-01'),
                    props('6').replace('<w:rPr/>', '<w:rPr><w:i/></w:rPr>'), props('6').replace('<w:b/>', '<w:i/>')]:
        failed, _ = check(tmp_path, old, [para(run('A', rpr=props('5')), run('B', rpr=changed))])
        assert failed
    failed, _ = check(tmp_path, [old[0], para(run('Unrelated', rpr='<w:rPr><w:b/></w:rPr>'))],
                      [new[0], para(run('Unrelated', rpr=props('8')))])
    assert 'existing_revisions_preserved' in failed and 'untracked_changes' in failed
    # A record on an empty run is still history, even beside the same author's other records.
    failed, _ = check(tmp_path, [para(run('A', rpr=props('5')), run('', rpr=props('9')))],
                      [para(run('A', rpr=props('5')))])
    assert 'existing_revisions_preserved' in failed


def test_tracked_edit_can_split_old_formatting_but_not_reuse_ids(tmp_path):
    def props(rid):
        return '<w:rPr><w:b/>' + rev('rPrChange', rid, 'Other', '<w:rPr/>') + '</w:rPr>'
    old = [para(run('AB', rpr=props('5')))]
    new = [para(run('A', rpr=props('5')),
                rev('del', '10', 'Ilya', run('B', 'delText', rpr=props('6')), date='2026-02-01T00:00:00Z'),
                rev('ins', '11', 'Ilya', run('X', rpr=props('7')), date='2026-02-01T00:00:00Z'))]
    failed, _ = check(tmp_path, old, new)
    assert not failed
    failed, _ = check(tmp_path, old, [new[0].replace('w:id="6"', 'w:id="5"')])
    assert 'new_revision_ids' in failed


def test_nested_table_in_deleted_outer_row_has_no_current_text(tmp_path):
    nested = '<w:tbl><w:tr><w:tc>' + para(run('Removed obligation')) + '</w:tc></w:tr></w:tbl>'
    table = ('<w:tbl><w:tr><w:trPr>' + rev('del', '7', 'Other') + '</w:trPr><w:tc>'
             + para(run('Deleted outer text')) + nested + para(run('Deleted tail')) + '</w:tc></w:tr></w:tbl>')
    path = docx(tmp_path / 'nested.docx', [table, para(run('Kept clause'))])
    items = read_all(path, details=True)
    assert [x['text'] for x in items if x['type'] == 'paragraph'] == ['', '', '', 'Kept clause']
    assert any(x['type'] == 'revision' and x['id'] == '7' for x in items)


@pytest.mark.parametrize('flag,expected', [('1', True), ('true', True), ('on', True), ('0', False), ('false', False), ('off', False)])
def test_reply_chain_and_resolved_status(tmp_path, flag, expected):
    parts = comments(*[f'<w:comment w:id="{i}" w:author="A"><w:p w14:paraId="{pid}">'
                       f'{run(text)}</w:p></w:comment>' for i, pid, text in [
                           (0, '000000AA', 'Question'), (1, '000000BB', 'Answer'), (2, '000000CC', 'Latest answer')]])
    parts['word/commentsExtended.xml'] = (f'<w15:commentsEx {NS}>'
        f'<w15:commentEx w15:paraId="000000AA" w15:done="{flag}"/>'
        '<w15:commentEx w15:paraId="000000CC" w15:paraIdParent="000000BB"/>'
        '<w15:commentEx w15:paraId="000000BB" w15:paraIdParent="000000AA"/></w15:commentsEx>')
    body = [para('<w:commentRangeStart w:id="0"/>', run('Payment clause'),
                 '<w:commentRangeEnd w:id="0"/>', '<w:r><w:commentReference w:id="0"/></w:r>')]
    path = docx(tmp_path / 'thread.docx', body, parts)
    found = [x for x in read_all(path, query='Payment clause', context=0) if x['type'] == 'comment']
    assert [c['id'] for c in found] == ['0', '1', '2']
    assert found[0]['resolved'] is expected
    assert found[2]['paragraphs'] == found[0]['paragraphs']
    assert read_docx(path, query='Latest answer', context=0)['matched_paragraphs'] == 1
    # Malformed cycles remain readable as unlocated comments, without looping or inventing anchors.
    parts['word/commentsExtended.xml'] = parts['word/commentsExtended.xml'].replace(
        'w15:paraIdParent="000000AA"', 'w15:paraIdParent="000000CC"')
    docx(path, body, parts)
    found = [x for x in read_all(path) if x['type'] == 'comment']
    assert [c['anchor_status'] for c in found] == ['located', 'not_located', 'not_located']


def test_change_excerpts_are_paged_and_do_not_reconstruct_rounds(tmp_path):
    old = [para(run(f'Clause {i}: old')) for i in range(12)]
    new = [para(run(f'Clause {i}: new')) for i in range(12)]
    _, result = check(tmp_path, old, new)
    assert result['changes']['total'] == 12 and result['changes']['next_start'] == 5
    assert len(result['changes']['items']) == 5


def test_change_excerpt_locates_edit_after_long_unchanged_prefix(tmp_path):
    prefix = 'Unchanged contract wording. ' * 30
    _, report = check(tmp_path, [para(run(prefix + '30 days'))], [para(run(prefix + '45 days'))])
    item = report['changes']['items'][0]
    assert item['before']['text_offset'] > 0
    assert item['before']['text_excerpt'].endswith('30 days')
    assert item['after']['text_excerpt'].endswith('45 days')


def test_direct_strikethrough_is_visible_and_search_keeps_original_words_and_revisions(tmp_path):
    strike = '<w:rPr><w:strike/></w:rPr>'
    double = '<w:rPr><w:dstrike w:val="on"/></w:rPr>'
    history = rev("rPrChange", "8", "Earlier author", "<w:rPr><w:strike/><w:dstrike/></w:rPr>")
    off = f'<w:rPr><w:strike w:val="false"/><w:dstrike w:val="0"/>{history}</w:rPr>'
    path = docx(tmp_path / "strike.docx", [para(run("Keep "), run("par", rpr=strike), run("tial", rpr=strike),
        run(" wording ", rpr=off), rev("del", "9", "Other", run("prior ", "delText", rpr=strike)),
        rev("ins", "10", "New author", run("double", rpr=double)), run(" ended", rpr="<w:rPr><w:dstrike w:val='off'/></w:rPr>"))])
    query = "partial wording double"
    output = read_document(path, query=query, context=0, details=True)
    assert "Keep [strike]partial[/strike] wording [double-strike]double[/double-strike] ended" in output
    assert "Earlier author" in output and "prior " in output and "New author" in output
    paragraph, *revisions = read_all(path, query=query, context=0, details=True)
    assert paragraph["text"] == "Keep partial wording double ended"
    spans = paragraph["strikethrough_spans"]
    assert [(span["kind"], paragraph["text"][span["start"]:span["end"]]) for span in spans] == [
        ("strike", "partial"), ("double_strike", "double")]
    assert {revision["id"] for revision in revisions} == {"8", "9", "10"}
    assert read_docx(path, query="prior", context=0)["matched_paragraphs"] == 1


def test_paragraph_tab_stop_is_not_text_but_run_tab_counts_in_strike_offsets(tmp_path):
    ppr = '<w:pPr><w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs></w:pPr>'
    strike = '<w:rPr><w:strike/></w:rPr>'
    body = para(run("Label"), "<w:r><w:tab/></w:r>", run("Old", rpr=strike), run(" wording"), ppr=ppr)
    path = docx(tmp_path / "tabs.docx", [body])

    paragraph, = read_all(path, query="Label Old", context=0)
    assert paragraph["text"] == "Label\tOld wording"
    span, = paragraph["strikethrough_spans"]
    assert span == {"start": 6, "end": 9, "kind": "strike"}
    assert paragraph["text"][span["start"]:span["end"]] == "Old"
    assert "p1 Label\t[strike]Old[/strike] wording" in read_document(path)


@pytest.mark.parametrize("strike,kind,tag", [("strike", "strike", "strike"), ("dstrike", "double_strike", "double-strike")])
def test_compare_manual_strikethrough_far_into_unchanged_paragraph(tmp_path, strike, kind, tag):
    prefix, word, suffix = "Unchanged " * 70, "changed-format", " still present"
    baseline = para(run(prefix + word + suffix))
    edited = para(run(prefix), run(word, rpr=f"<w:rPr><w:{strike}/></w:rPr>"), run(suffix))
    failed, report = check(tmp_path, [baseline], [edited])
    assert "untracked_changes" in failed
    item, = report["changes"]["items"]
    assert item["kind"] == "formatting_or_markup"
    left, right = item["before"], item["after"]
    assert left["id"] == right["id"] == "word/document.xml#p1"
    assert left["text_excerpt"] == right["text_excerpt"] and word in left["text_excerpt"]
    assert right["text_offset"] == len(prefix) - 70
    assert f"[{tag}]{word}[/{tag}]" in right["formatted_excerpt"]
    assert "strikethrough_spans" not in left
    assert right["strikethrough_spans"] == [{"start": len(prefix), "end": len(prefix + word), "kind": kind}]


def _cell(*body):
    return f"<w:tc>{''.join(body)}</w:tc>"


def _row(*cells):
    return f"<w:tr>{''.join(cells)}</w:tr>"


def _table(*rows):
    return f"<w:tbl>{''.join(rows)}</w:tbl>"


def test_table_row_expands_both_nine_paragraph_cells_in_physical_order(tmp_path):
    left = [f"English paragraph {i}: " + "original provisions " * 12 for i in range(1, 10)]
    right = [f"Русский абзац {i}: " + "условия договора " * 12 for i in range(1, 10)]
    notes = comments(rev("comment", "2", "Reviewer", para(run("Payment note"))),
                     rev("comment", "3", "Reviewer", para(run("Unlocated note"))))
    left[5] += " UNIQUE"
    right_nodes = [para(run(text)) for text in right]
    right_nodes[5] = para('<w:commentRangeStart w:id="2"/>', run(right[5]),
                          rev("del", "8", "Editor", run("old", "delText")),
                          rev("ins", "9", "Editor", run("new")),
                          '<w:commentRangeEnd w:id="2"/>')
    path = docx(tmp_path / "table.docx", [para(run("Earlier text")),
        _table(_row(_cell(*(para(run(text)) for text in left)), _cell(*right_nodes)),
               _row(_cell(para(run("Other row")))))], notes)
    baseline = path.read_bytes()

    found = read_docx(path, query="UNIQUE", context=0)["items"]
    hit, = found
    assert hit["id"] == "word/document.xml#p7"
    assert hit["location"]["neighbor_cell_excerpts"] == [{"cell": 2, "text": right[0][:100]}]
    compact_search = read_document(path, query="UNIQUE", context=0)
    assert "c2 excerpt (up to 100 characters)" in compact_search
    assert right[5] not in compact_search and "table_row='p7'" in compact_search

    result = read_docx(path, details=True, table_row="p7")
    assert result == read_docx(path, details=True, table_row=hit["id"], context=50)
    assert result["table_row"] == {"part": "word/document.xml", "table": 1, "row": 1,
                                   "paragraph": hit["id"], "cells": 2}
    assert result["order"] == "table_row"
    cells = [item["location"]["cell"] for item in result["items"] if item["type"] == "table_cell"]
    paragraphs = [item for item in result["items"] if item["type"] == "paragraph"]
    assert cells == [1, 2]
    assert [item["location"]["cell"] for item in paragraphs] == [1] * 9 + [2] * 9
    assert [item["id"] for item in paragraphs] == [f"word/document.xml#p{i}" for i in range(2, 20)]
    right[5] += "new"
    assert [item["text"] for item in paragraphs] == left + right
    assert [item["id"] for item in result["items"] if item["type"] == "revision"] == ["8", "9"]
    assert [item["id"] for item in result["items"] if item["type"] == "comment"] == ["2"]
    compact = read_document(path, table_row="p7")
    assert compact.index("[body t1 r1 c1]") < compact.index(left[0]) < compact.index("[body t1 r1 c2]")
    assert right[-1] in compact and "Other row" not in compact and "excerpt (up to" not in compact
    assert path.read_bytes() == baseline


def test_table_row_uses_nearest_nested_row_and_preserves_nested_text_in_outer_row(tmp_path):
    wrapped_cell = '<w:sdt><w:sdtContent>' + _cell(para(run("Inner A"))) + '</w:sdtContent></w:sdt>'
    nested = _table(_row(wrapped_cell, _cell(para(run("Inner B")))),
                    _row(_cell(para(run("Inner other row")))))
    outer = _table('<w:customXml>' + _row(_cell(para(run("Outer before")), nested, para(run("Outer after"))),
                                         _cell(para(run("Outer right")))) + '</w:customXml>',
                   _row(_cell(para(run("Outer other row")))))
    path = docx(tmp_path / "nested.docx", [outer])

    inner = read_docx(path, table_row="p3")
    assert (inner["table_row"]["table"], inner["table_row"]["row"]) == (2, 1)
    assert [item["text"] for item in inner["items"] if item["type"] == "paragraph"] == ["Inner A", "Inner B"]

    outer = read_docx(path, table_row="p1")
    assert (outer["table_row"]["table"], outer["table_row"]["row"]) == (1, 1)
    assert [item["text"] for item in outer["items"] if item["type"] == "paragraph"] == [
        "Outer before", "Inner A", "Inner B", "Inner other row", "Outer after", "Outer right"]
    output = read_document(path, table_row="p1")
    assert output.index("[body t1 r1 c1]") < output.index("[body t2 r1 c1]") < output.index("[body t1 r1 c2]")


def test_table_row_full_address_selects_header_and_retains_empty_cell_boundary(tmp_path):
    header = f'<w:hdr {NS}>{_table(_row(_cell(para(run("In header")), _cell()) ))}</w:hdr>'
    path = docx(tmp_path / "header.docx", [para(run("In body"))], {"word/header1.xml": header})
    found, = read_all(path, query="In header", context=0)
    result = read_docx(path, table_row=found["id"])
    assert [item["type"] for item in result["items"]] == ["table_cell", "paragraph", "table_cell"]
    compact = read_document(path, table_row=found["id"])
    assert "s1:pN = word/header1.xml#pN" in compact
    assert "[s1: t1 r1 c2]" in compact and "In body" not in compact


@pytest.mark.parametrize("options, reason", [
    ({"table_row": "p1"}, "outside a table row"),
    ({"table_row": "p999"}, "not found"),
    ({"table_row": 1}, "paragraph address"),
    ({"table_row": "p1", "query": "word"}, "either table_row or query"),
])
def test_invalid_table_row_is_reported_explicitly(tmp_path, options, reason):
    path = docx(tmp_path / "outside.docx", [para(run("Not a table"))])
    with pytest.raises(ValueError, match=reason):
        read_document(path, **options)


def test_full_table_row_output_is_available_through_the_existing_cursor(project):
    import asyncio

    from veqtor_workspace.operations import Operations

    workspace, _ = project
    long_text = "полный текст ячейки " * 1000
    path = docx(workspace.project / "row.docx",
                [_table(_row(_cell(para(run("Source"))), _cell(para(run(long_text)))))])
    expected = read_document(path, table_row="p1") + "\n"

    async def exercise():
        operations = Operations(workspace)
        report = await operations.call("run_python", {"code":
            f"from veqtor_workspace.reading import read_docx; print(read_docx({str(path)!r}, table_row='p1'))"})
        assert report["exit_code"] == 0
        text, cursor = report["stdout"], report["stdout_next_cursor"]
        assert cursor
        while cursor:
            page = await operations.call("run_python", {"output_cursor": cursor})
            text += page["text"]
            cursor = page["next_cursor"]
        assert text == expected
    asyncio.run(exercise())


@pytest.mark.parametrize("old", ["Old right of return.", "Old right of return. " * 10])
def test_neighbour_cell_excerpt_marks_direct_strike_and_clips_raw_text(tmp_path, old):
    prefix, suffix = "Keep. ", " End."
    left = _cell(para(run("The right to a refund")))
    right = _cell(para(run(prefix), run(old, rpr="<w:rPr><w:strike/></w:rPr>"), run(suffix)))
    path = docx(tmp_path / "neighbour.docx", [_table(_row(left, right))])
    end = min(len(prefix + old), 100)

    query = "The right to a refund"
    data, = read_all(path, query=query, context=0)
    excerpt, = data["location"]["neighbor_cell_excerpts"]
    assert excerpt["text"] == (prefix + old + suffix)[:100]
    assert excerpt["strikethrough_spans"] == [{"start": len(prefix), "end": end, "kind": "strike"}]
    marked = prefix + f"[strike]{old[:end - len(prefix)]}[/strike]" + excerpt["text"][end:]
    compact = read_document(path, query=query, context=0)
    assert f"c2 excerpt (up to 100 characters): {marked}" in compact
    neighbour, = read_all(path, query="Old right of return", context=0)
    assert neighbour["text"] == prefix + old + suffix


@pytest.mark.parametrize("with_fallback", [True, False])
def test_alternate_cells_use_only_fallback_and_keep_xml_paragraph_ids(tmp_path, with_fallback):
    fallback = f'<mc:Fallback>{_cell(para(run("Stored fallback")))}</mc:Fallback>' if with_fallback else ""
    alternate = ('<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
                 'xmlns:qaAlt="urn:test:unsupported"><mc:Choice Requires="qaAlt">'
                 f'{_cell(para(run("Unsupported variant")))}</mc:Choice>{fallback}</mc:AlternateContent>')
    body = [_table(_row(_cell(para(run("Find this cell"))), alternate)), para(run("After table"))]
    path = docx(tmp_path / "alternate.docx", body)
    first_id = "word/document.xml#p1"
    expected = ["Find this cell"] + (["Stored fallback"] if with_fallback else [])

    whole = read_docx(path)
    paragraphs = [item for item in whole["items"] if item["type"] == "paragraph"]
    assert [p["text"] for p in paragraphs] == expected + ["After table"]
    assert [p["location"]["paragraph"] for p in paragraphs] == ([1, 3, 4] if with_fallback else [1, 3])
    coverage = [{"part": "word/document.xml", "fallbacks_used": int(with_fallback),
                 "omitted_without_fallback": int(not with_fallback)}]
    assert whole["alternate_content"] == coverage

    row = read_docx(path, table_row=first_id)
    assert row["table_row"]["cells"] == len(expected) and row["alternate_content"] == coverage
    assert [item["text"] for item in row["items"] if item["type"] == "paragraph"] == expected
    assert [item["location"]["cell"] for item in row["items"] if item["type"] == "table_cell"] == list(
        range(1, len(expected) + 1))
    match, = read_all(path, query="Find this cell", context=0)
    excerpts = [{"cell": 2, "text": "Stored fallback"}] if with_fallback else []
    assert match["location"]["neighbor_cell_excerpts"] == excerpts
    assert read_docx(path, query="Unsupported variant", context=0)["matched_paragraphs"] == 0

    compact = read_document(path, table_row="p1")
    assert "Unsupported variant" not in compact and "[body t1 r1 c3]" not in compact
    assert "Word may choose a different branch" in compact
    if with_fallback:
        found, = read_all(path, query="Stored fallback", context=0)
        assert found["id"] == "word/document.xml#p3" and found["location"]["cell"] == 2
        assert read_docx(path, table_row=found["id"])["items"] == row["items"]
        assert "c2 excerpt (up to 100 characters): Stored fallback" in read_document(path, query="Find this cell", context=0)
        assert "stored fallbacks used: 1" in compact
    else:
        assert "omitted without a fallback: 1" in compact and "[body t1 r1 c2]" not in compact


def test_inline_compatibility_choice_does_not_enter_text_or_revision_search(tmp_path):
    alternate = ('<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
                 'xmlns:qaAlt="urn:test:unsupported"><mc:Choice Requires="qaAlt">'
                 + rev("ins", "1", "Editor", run("discarded"))
                 + '</mc:Choice><mc:Fallback>' + run("used") + '</mc:Fallback></mc:AlternateContent>')
    path = docx(tmp_path / "inline.docx", [para(run("Prefix "), alternate)])
    paragraph, = read_all(path, details=True)
    assert paragraph["text"] == "Prefix used" and paragraph["revision_count"] == 0
    assert read_docx(path, query="discarded", context=0)["matched_paragraphs"] == 0


def test_row_hint_uses_the_matching_paragraph_instead_of_context(tmp_path):
    path = docx(tmp_path / "hint.docx", [para(run("Intro hit")),
        _table(_row(_cell(para(run("Table hit"))), _cell(para(run("Adjacent")))))])
    outside = read_document(path, query="Intro hit", context=1)
    assert "p2 Table hit" in outside and "table_row=" not in outside

    inside = read_document(path, query="Table hit", context=1)
    assert "table_row='p2'" in inside
    assert "Read all cells" in read_document(path, query="Table hit", context=0)


def _property_alternate(choice, fallback=None):
    stored = "" if fallback is None else f"<mc:Fallback>{fallback}</mc:Fallback>"
    return ('<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
            'xmlns:qaAlt="urn:test:unsupported"><mc:Choice Requires="qaAlt">'
            f"{choice}</mc:Choice>{stored}</mc:AlternateContent>")


def test_fallback_run_properties_match_expanded_strikethrough_and_preserve_raw_differences(tmp_path):
    history = rev("rPrChange", "8", "Former editor", "<w:rPr><w:strike/><w:dstrike/></w:rPr>")
    properties = [
        ("single", "<w:dstrike/>", "<w:strike/>"),
        (" double", "<w:strike/>", "<w:dstrike w:val='on'/>"),
        (" retained", "<w:strike/><w:dstrike/>", "<w:strike w:val='off'/><w:dstrike w:val='0'/>" + history),
    ]

    def body(wrapped):
        runs = [run(text, rpr=f"<w:rPr>{_property_alternate(choice, current) if wrapped else current}</w:rPr>")
                for text, choice, current in properties]
        return [para(run("Keep "), *runs)]

    original = docx(tmp_path / "original.docx", [para(run("Keep single double retained"))])
    selected = docx(tmp_path / "selected.docx", body(True))
    expanded = docx(tmp_path / "expanded.docx", body(False))
    before = selected.read_bytes()
    query = "single double retained"
    items = read_all(selected, query=query, context=0, details=True)
    assert items == read_all(expanded, query=query, context=0, details=True)
    paragraph, previous = items
    assert paragraph["text"] == "Keep single double retained"
    assert paragraph["strikethrough_spans"] == [{"start": 5, "end": 11, "kind": "strike"},
                                               {"start": 11, "end": 18, "kind": "double_strike"}]
    assert previous["kind"] == "rPrChange" and previous["author"] == "Former editor"
    for path in (selected, expanded):
        assert "Keep [strike]single[/strike][double-strike] double[/double-strike] retained" in read_document(
            path, query=query, context=0)
    selected_changes = check_docx(original, selected, "Editor")["changes"]
    expanded_changes = check_docx(original, expanded, "Editor")["changes"]
    assert selected_changes["items"] == expanded_changes["items"]
    raw = check_docx(selected, expanded, "Editor")
    assert "word/document.xml" in raw["changed_parts"] and "untracked_changes" in raw["summary"]["observations"]
    assert selected.read_bytes() == before


@pytest.mark.parametrize("deletion_in", ["fallback", "choice", "history"])
def test_fallback_row_properties_match_expanded_text_and_excerpts(tmp_path, deletion_in):
    deleted = rev("del", "7", "Earlier editor")
    current = {"fallback": deleted, "choice": "<w:cantSplit/>",
               "history": rev("trPrChange", "8", "Former editor", f"<w:trPr>{deleted}</w:trPr>")}[deletion_in]
    choice = "<w:cantSplit/>" if deletion_in == "fallback" else deleted

    def table(properties, terms):
        return [_table(_row(f"<w:trPr>{properties}</w:trPr>", _cell(para(run("Payment"))), _cell(para(run(terms))))),
                para(run("Following paragraph"))]

    original = docx(tmp_path / "original.docx", table("", "Old terms"))
    selected = docx(tmp_path / "selected.docx", table(_property_alternate(choice, current), "Current terms"))
    expanded = docx(tmp_path / "expanded.docx", table(current, "Current terms"))
    row = read_all(selected, table_row="p1", details=True)
    assert row == read_all(expanded, table_row="p1", details=True)
    expected = "" if deletion_in == "fallback" else "Current terms"
    left, right = [item for item in row if item["type"] == "paragraph"]
    assert left["text"] == ("" if deletion_in == "fallback" else "Payment")
    assert right["location"]["paragraph"] == 2 and right["text"] == expected
    assert left["location"]["neighbor_cell_excerpts"] == [{"cell": 2, "text": expected}]
    for path in (selected, expanded):
        assert ("Current terms" in read_document(path, table_row="p1")) == bool(expected)
        assert read_docx(path, query="Current terms", context=0)["matched_paragraphs"] == bool(expected)
        changes = check_docx(original, path, "Editor")["changes"]["items"]
        assert [item["after"]["id"] for item in changes] == (
            ["word/document.xml#p1", "word/document.xml#p2"] if deletion_in == "fallback" else ["word/document.xml#p2"])
        assert changes[-1]["after"]["text_excerpt"] == expected


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("restored", [False, True])
def test_isolated_row_deletion_changes_projected_paragraphs_in_both_directions(tmp_path, wrapped, restored):
    mark = rev("del", "73", "Editor")
    if wrapped:
        mark = _property_alternate("<w:cantSplit/>", mark)
    live = _row(_cell(para(run("Repeated wording"))), _cell(para(run("Payment clause"))))
    deleted = live.replace("<w:tr>", f"<w:tr><w:trPr>{mark}</w:trPr>", 1)
    # Repeating the same raw paragraph next to the edited row must not redirect the match.
    neighbour = _row(_cell(para(run("Repeated wording"))))
    before, after = (deleted, live) if restored else (live, deleted)
    original = docx(tmp_path / "original.docx", [_table(_row(_cell(para(run("Earlier row")))), before, neighbour)])
    candidate = docx(tmp_path / "candidate.docx", [_table(_row(_cell(para(run("Earlier row")))), after, neighbour)])
    report = check_docx(original, candidate, "Editor")
    assert report["changed_parts"] == ["word/document.xml"]
    found = report["changes"]
    assert found["total"] == 2 and found["next_start"] is None
    for cell, (item, text) in enumerate(zip(found["items"], ("Repeated wording", "Payment clause")), 1):
        assert item["kind"] == "replace"
        left, right = item["before"], item["after"]
        assert (left["text_excerpt"], right["text_excerpt"]) == (("", text) if restored else (text, ""))
        for side in (left, right):
            assert side["id"] == f"word/document.xml#p{cell + 1}"
            assert (side["location"]["table"], side["location"]["row"], side["location"]["cell"]) == (1, 2, cell)
