# SPDX-License-Identifier: Apache-2.0
import pytest
from test_checks import ME, OTHER, check, para, rev, run

from veqtor_workspace.text_edits import minimal_edits


@pytest.mark.parametrize("before,after", [
    ("within 30 days", "within 45 days"),
    ("это необходимая", "— это необходимая"),
    ("Licensor", "Licensee"), ("same", "same"), ("", "new"), ("remove", ""),
    ("A\u00a0B", "A B"), ("N-1 из N-2", "N-1 — из N-2"),
    ("Сроки рассмотрения можно согласовать нам внутри с соответствующими коллегам;",
     "Сроки рассмотрения можно согласовать внутри команды с техническими коллегами;"),
])
def test_minimal_edits_roundtrip(before, after):
    result = before
    for edit in reversed(minimal_edits(before, after)):
        assert before[edit["start"]:edit["end"]] == edit["old"]
        result = result[:edit["start"]] + edit["new"] + result[edit["end"]:]
    assert result == after


def test_exact_edit_spans():
    assert minimal_edits("within 30 days", "within 45 days") == [{"start": 7, "end": 9, "old": "30", "new": "45"}]
    assert minimal_edits("это необходимая", "— это необходимая") == [{"start": 0, "end": 0, "old": "", "new": "— "}]
    assert minimal_edits("Licensor", "Licensee") == [{"start": 0, "end": 8, "old": "Licensor", "new": "Licensee"}]
    edits = minimal_edits("нам внутри с соответствующими коллегам", "внутри команды с техническими коллегами")
    assert all("внутри" not in edit["old"] + edit["new"] for edit in edits)


def test_quality_reports_equal_text_spanning_multiple_deletions(tmp_path):
    original = [para(run("это необходимая"))]
    candidate = [para(rev("del", "1", ME, run("это", "delText")),
                      rev("ins", "2", ME, run("— это необходимая")),
                      rev("del", "3", ME, run(" необходимая", "delText")))]
    defects, report = check(tmp_path, original, candidate)
    assert not defects
    assert "redline_quality" in report["summary"]["warnings"]
    quality = next(c["detail"] for c in report["checks"] if c["check"] == "redline_quality")
    assert quality["findings"][0]["unchanged_suffix"] == "это необходимая"


@pytest.mark.parametrize("scenario", ["precise", "format", "history", "nested", "move"])
def test_quality_does_not_demand_rewriting_valid_or_complex_changes(tmp_path, scenario):
    old = [para(run("same"))]
    if scenario == "precise":
        new = [para(rev("ins", "1", ME, run("— ")), run("same"))]
    elif scenario == "format":
        new = [para(rev("del", "1", ME, run("same", "delText")),
                    rev("ins", "2", ME, run("same", rpr="<w:rPr><w:b/></w:rPr>")))]
    elif scenario == "history":
        old = new = [para(rev("del", "1", OTHER, run("same", "delText")), rev("ins", "2", OTHER, run("same")))]
    elif scenario == "nested":
        old = [para(rev("ins", "1", OTHER, run("same")))]
        new = [para(rev("ins", "1", OTHER, rev("del", "2", ME, run("same", "delText"))),
                    rev("ins", "3", ME, run("same")))]
    else:
        new = [para(rev("moveFrom", "1", ME, run("same")), rev("moveTo", "2", ME, run("same")))]
    _, report = check(tmp_path, old, new)
    assert "redline_quality" not in report["summary"]["warnings"]
