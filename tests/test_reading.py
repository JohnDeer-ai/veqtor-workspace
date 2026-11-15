# SPDX-License-Identifier: Apache-2.0
"""Read evidence, not a reconstructed negotiation history or a Word layout."""

import json
from collections import Counter

import pytest
from test_checks import NS, check, comments, docx, mark, math, para, rev, run

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
    assert found[0]['anchor_markup_excerpt'] == 'firstsecond'
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
