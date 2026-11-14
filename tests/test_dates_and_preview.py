# SPDX-License-Identifier: Apache-2.0
"""Advisory dates and a text fallback for ambiguous rendered revisions."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_checks import ME, OTHER, anchored, check, comments, docx, para, rev, run
from test_reading import read_all

from veqtor_workspace import checks, runner
from veqtor_workspace.checks import check_docx
from veqtor_workspace.reading import NESTED_DELETION_WARNING


class Clock(datetime):
    instant = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(Clock, "instant", datetime(2026, 11, 14, 12, tzinfo=timezone.utc))
    monkeypatch.setattr(checks, "datetime", Clock)
    monkeypatch.setattr(runner, "datetime", Clock)
    return Clock


def test_python_gets_fresh_utc_per_call(project, clock):
    workspace, _ = project
    code = "import os; print(os.environ['VEQTOR_AUTHOR'], os.environ['VEQTOR_UTC_NOW'])"
    first = workspace.run(code)
    assert first['exit_code'] == 0, first
    assert first['stdout'] == 'Ilya 2026-11-14T12:00:00Z\n'
    clock.instant = datetime(2026, 11, 14, 12, 4, tzinfo=timezone.utc)
    second = workspace.run(code)
    assert second['exit_code'] == 0, second
    assert second['stdout'] == 'Ilya 2026-11-14T12:04:00Z\n'


@pytest.mark.parametrize('date,reason', [
    ('2026-11-14T12:00:00Z', None),
    ('2026-11-14T15:00:00+03:00', None),
    ('2026-11-13T12:00:00+00:00', None),
    ('2026-11-13T12:00:00-00:00', None),
    ('2026-11-13T12:00:00+14:00', None),
    ('2026-11-13T12:00:00-14:00', None),
    ('2026-11-13T12:00:00+13:59', None),
    ('2026-11-13T12:00:00-03:30', None),
    ('2026-11-14T12:05:00Z', None),
    (None, 'Missing date'),
    ('not-a-date', 'Invalid date/time'),
    ('2026-11-14T12:00:00', 'explicit timezone'),
    ('2026-11-14T10:00:00+03:75', 'valid explicit timezone'),
    ('2026-11-14T10:00:00-03:75', 'valid explicit timezone'),
    ('2026-11-14T10:00:00+14:01', 'valid explicit timezone'),
    ('2026-11-14T10:00:00-14:01', 'valid explicit timezone'),
    ('2026-11-14T10:00:00+15:00', 'valid explicit timezone'),
    ('2026-11-14T10:00:00-15:00', 'valid explicit timezone'),
    ('2026-11-14T10:00:00+0300', 'valid explicit timezone'),
    ('2026-11-14T10:00:00+03:00:30', 'valid explicit timezone'),
    ('2026-11-14T12:05:01Z', 'future'),
    ('2026-11-14T15:00:00Z', 'future'),
])
def test_only_new_revision_and_comment_dates_warn(tmp_path, clock, date, reason):
    old = para(rev('ins', '0', OTHER, run('History'), date='2099-01-01T00:00:00Z'))
    old_comment = rev('comment', '0', OTHER, para(run('Keep')), date='unknown')
    original = [old, anchored('0', 'First'), para(run('Clause'))]
    candidate = [old, anchored('0', 'First'),
                 para(run('Clause'), rev('ins', '1', ME, run(' added'), date=date)), anchored('1', 'Note')]
    _, report = check(tmp_path, original, candidate, comments(old_comment),
                      comments(old_comment, rev('comment', '1', ME, para(run('New note')), date=date)))
    by_name = {c['check']: c for c in report['checks']}
    assert report['checked_at_utc'] == '2026-11-14T12:00:00Z'
    assert report['native_word'] == 'not_checked_by_server'
    for name in ('new_revision_dates', 'new_comment_dates'):
        finding = by_name[name]
        if reason is None:
            assert finding['status'] == 'passed', finding
        else:
            assert finding['status'] == 'warning', finding
            assert finding['detail']['count'] == 1
            issue = finding['detail']['issues'][0]
            assert issue['id'] == '1' and issue['date'] == date
            assert reason in issue['issue']
    assert 'existing_comments_preserved' not in report['summary']['observations']


@pytest.mark.parametrize('date', ['2099-01-01T00:00:00Z', '2026-11-14T10:00:00+03:75'])
def test_date_warning_does_not_rewrite_or_block_save(project, clock, date):
    workspace, tasks = project
    source = docx(workspace.project / 'source.docx', [para(run('Original'))])
    original = source.read_bytes()
    task = tasks.start(str(source))
    candidate = Path(task['cwd']) / task['candidate']
    docx(candidate, [para(run('Original'), rev('ins', '1', ME, run(' added'), date=date))])
    proposed = candidate.read_bytes()
    report = check_docx(Path(task['snapshot']), candidate, ME)
    assert report['summary']['warnings'] == ['new_revision_dates']
    assert candidate.read_bytes() == proposed and source.read_bytes() == original
    assert tasks.save(task['task_id'])['status'] == 'saved'
    assert source.read_bytes() == proposed
    assert Path(task['snapshot']).read_bytes() == original


def test_nested_deletion_has_location_and_proposed_text(tmp_path):
    old = para(run('8.7 Amount is '), rev('ins', '5', OTHER, run('debited')), run(' to account.'))
    edited = para(run('8.7 Amount is '), rev('ins', '5', OTHER,
                  rev('del', '6', ME, run('debited', 'delText'))),
                  rev('ins', '7', ME, run('credited')), run(' to account.'))
    def table(p):
        return '<w:tbl><w:tr><w:tc>' + p + '</w:tc></w:tr></w:tbl>'

    failed, report = check(tmp_path, [table(old)], [table(edited)])
    assert not failed, report
    guidance = report['preview_guidance']
    assert guidance['affected_paragraphs'] == 1
    fragment = guidance['fragments'][0]
    assert fragment['id'] == 'word/document.xml#p1'
    assert {k: fragment['location'][k] for k in ('table', 'row', 'cell')} == {'table': 1, 'row': 1, 'cell': 1}
    assert fragment['proposed_text'] == '8.7 Amount is credited to account.'
    assert fragment['text_complete'] and guidance['remaining_paragraphs'] == 0
    assert 'not a clean DOCX or Word layout' in guidance['text_view']
    candidate = tmp_path / 'candidate.docx'
    before = candidate.read_bytes()
    item = read_all(candidate, query='8.7', context=0)[0]
    assert item['text'] == fragment['proposed_text']
    assert NESTED_DELETION_WARNING in item['warnings']
    assert candidate.read_bytes() == before


def test_plain_deletion_has_no_nested_warning(tmp_path):
    body = [para(run('A '), rev('del', '1', ME, run('B', 'delText')), run('C'))]
    _, report = check(tmp_path, body, body)
    assert report['preview_guidance'] == {'document': 'candidate', 'affected_paragraphs': 0, 'fragments': []}
    assert NESTED_DELETION_WARNING not in read_all(tmp_path / 'candidate.docx')[0]['warnings']


def test_preview_excerpt_limits_have_readable_continuations(tmp_path):
    body = [para(rev('ins', str(i), OTHER, run(f'Clause {i} ' + 'long text ' * 110),
                      rev('del', str(i + 20), ME, run('gone', 'delText')))) for i in range(7)]
    _, report = check(tmp_path, body, body)
    guidance = report['preview_guidance']
    assert guidance['affected_paragraphs'] == 7 and guidance['remaining_paragraphs'] == 2
    assert len(guidance['fragments']) == 5
    assert all(len(f['proposed_text']) == 900 and not f['text_complete'] for f in guidance['fragments'])
    assert 'next_cursor' in guidance['read_more']
    full = ''.join(x['text'] for x in read_all(tmp_path / 'candidate.docx') if x['type'] == 'paragraph')
    assert full == ''.join(f'Clause {i} ' + 'long text ' * 110 for i in range(7))
    assert 'gone' not in full


def test_unreadable_package_does_not_imply_native_word_check(tmp_path):
    broken = tmp_path / 'broken.docx'
    broken.write_bytes(b'not a ZIP')
    report = check_docx(broken, broken, ME)
    assert report['status'] == 'error'
    assert report['native_word'] == 'not_checked_by_server'
    assert report['checked_at_utc'].endswith('Z')


def test_preview_prioritizes_changed_passage_over_old_history(tmp_path):
    old = [para(rev('ins', str(i), OTHER, run(f'Old {i}'),
                    rev('del', str(i + 20), ME, run('gone', 'delText')))) for i in range(6)]
    original = [*old, para(rev('ins', '40', OTHER, run('debited')))]
    candidate = [*old, para(rev('ins', '40', OTHER, rev('del', '41', ME, run('debited', 'delText'))),
                           rev('ins', '42', ME, run('credited')))]
    failed, report = check(tmp_path, original, candidate)
    assert not failed, report
    guidance = report['preview_guidance']
    assert guidance['affected_paragraphs'] == 7
    assert guidance['fragments'][0]['id'] == 'word/document.xml#p7'
    assert guidance['fragments'][0]['proposed_text'] == 'credited'


@pytest.mark.parametrize('position', [0, 3, 6])
def test_preview_prioritizes_new_occurrence_of_old_fragment(tmp_path, position):
    old = [para(rev('ins', str(i), OTHER, run(f'Old {i} '),
                    rev('del', str(i + 20), ME, run('gone', 'delText')))) for i in range(6)]
    duplicate = para(rev('ins', '100', OTHER, run('Old 0 '),
                        rev('del', '101', ME, run('gone', 'delText'))))
    _, report = check(tmp_path, old, [*old[:position], duplicate, *old[position:]])
    guidance = report['preview_guidance']
    assert guidance['affected_paragraphs'] == 7 and guidance['remaining_paragraphs'] == 2
    assert guidance['fragments'][0]['id'] == f'word/document.xml#p{position + 1}'
    assert guidance['fragments'][0]['proposed_text'] == 'Old 0 '
    assert report['changes']['total'] == 1
    assert report['changes']['items'][0]['after']['id'] == guidance['fragments'][0]['id']
    full = [item for item in read_all(tmp_path / 'candidate.docx') if item['type'] == 'paragraph']
    assert len(full) == 7 and sum(item['text'] == 'Old 0 ' for item in full) == 2


def test_preview_prioritizes_changed_occurrence_matching_another_old_paragraph(tmp_path):
    old = [para(rev('ins', str(i), OTHER, run(f'Old {i} '),
                    rev('del', str(i + 20), ME, run('gone', 'delText')))) for i in range(6)]
    duplicate = para(rev('ins', '100', OTHER, run('Old 0 '),
                        rev('del', '101', ME, run('gone', 'delText'))))
    _, report = check(tmp_path, old, [*old[:5], duplicate])
    assert report['preview_guidance']['fragments'][0]['id'] == 'word/document.xml#p6'


def test_deleted_paragraph_does_not_make_remaining_history_new(tmp_path):
    old = [para(rev('ins', str(i), OTHER, run(f'Old {i} '),
                    rev('del', str(i + 20), ME, run('gone', 'delText')))) for i in range(7)]
    _, report = check(tmp_path, old, old[1:])
    assert report['preview_guidance']['fragments'][0]['proposed_text'] == 'Old 1 '
    assert report['changes']['total'] == 1
    assert report['changes']['items'][0]['after'] is None
