# SPDX-License-Identifier: Apache-2.0
"""Read evidence, not a reconstructed negotiation history or a Word layout."""

import json

import pytest
from test_checks import NS, check, comments, docx, mark, para, rev, run

from veqtor_workspace.reading import read_docx


def read_all(path, **options):
    items, cursor = [], None
    while True:
        page = read_docx(path, cursor=cursor, **options)
        assert len(json.dumps(page).encode()) <= 10_000
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return items


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


def test_long_unicode_text_continues_and_changed_source_invalidates_cursor(tmp_path):
    text = "Очень длинное условие договора. " * 500
    path = docx(tmp_path / "long.docx", [para(run(text))])
    first = read_docx(path)
    assert first["next_cursor"]
    pieces = [x for x in read_all(path) if x["type"] == "paragraph"]
    assert "".join(x["text"] for x in pieces) == text
    assert pieces[-1]["text_continues"] is False
    with pytest.raises(ValueError, match="cursor"):
        read_docx(path, query="different", cursor=first["next_cursor"])
    docx(path, [para(run(text + "changed"))])
    with pytest.raises(ValueError, match="cursor"):
        read_docx(path, cursor=first["next_cursor"])


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


def test_mixed_same_stamp_insertions_are_explicitly_unchecked(tmp_path):
    old = [para(rev('ins', '1', 'Ilya', run('A B'))), para(run('Untouched'))]
    mixed = para(rev('ins', '1', 'Ilya', run('A ')), rev('ins', '2', 'Ilya', run('X ')),
                 rev('ins', '3', 'Ilya', run('B')))
    failed, report = check(tmp_path, old, [mixed, old[1]])
    assert not failed
    assert {'existing_revisions_preserved', 'untracked_changes', 'revision_origins'} <= set(report['summary']['not_checked'])
    origins = next(c['detail'] for c in report['checks'] if c['check'] == 'revision_origins')
    assert origins[0]['paragraph'] == 1 and origins[0]['preservation_not_checked'] is True
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


def test_nested_table_in_deleted_outer_row_has_no_current_text(tmp_path):
    nested = '<w:tbl><w:tr><w:tc>' + para(run('Removed obligation')) + '</w:tc></w:tr></w:tbl>'
    table = ('<w:tbl><w:tr><w:trPr>' + rev('del', '7', 'Other') + '</w:trPr><w:tc>'
             + para(run('Deleted outer text')) + nested + para(run('Deleted tail')) + '</w:tc></w:tr></w:tbl>')
    path = docx(tmp_path / 'nested.docx', [table, para(run('Kept clause'))])
    items = read_all(path, details=True)
    assert [x['text'] for x in items if x['type'] == 'paragraph'] == ['', '', '', 'Kept clause']
    assert any(x['type'] == 'revision' and x['id'] == '7' for x in items)


@pytest.mark.parametrize('flag,expected', [('1', True), ('true', True), ('0', False), ('false', False)])
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
