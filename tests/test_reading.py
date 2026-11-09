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
