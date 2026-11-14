# SPDX-License-Identifier: Apache-2.0
"""Bounded observations, not a universal DOCX validator or a publication gate.

Core idea: undo every new tracked change and every new comment in the candidate.
What remains must equal the original, apart from harmless differences (run splits,
rsid/paraId attributes, proofing marks, property order). Anything else is reported
as an untracked change, with its location.
"""

from __future__ import annotations

import difflib
import hashlib
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from lxml import etree as ET

from veqtor_workspace.text_edits import TOKEN

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
W14 = "{http://schemas.microsoft.com/office/word/2010/wordml}"
W15 = "{http://schemas.microsoft.com/office/word/2012/wordml}"
W16CID = "{http://schemas.microsoft.com/office/word/2016/wordml/cid}"
W16CEX = "{http://schemas.microsoft.com/office/word/2018/wordml/cex}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def _w(*names):
    return frozenset(W + name for name in names)


def _short(tag):
    return tag.rsplit("}", 1)[-1]


TEXT_REVISIONS = _w("ins", "del", "moveFrom", "moveTo")
# Formatting change element -> the property container it belongs to and restores.
PROPERTY_CHANGES = {W + change: W + container for change, container in (
    ("rPrChange", "rPr"), ("pPrChange", "pPr"), ("tcPrChange", "tcPr"), ("trPrChange", "trPr"),
    ("tblPrChange", "tblPr"), ("tblPrExChange", "tblPrEx"), ("tblGridChange", "tblGrid"),
    ("sectPrChange", "sectPr"))}
# Children of a property container that its change element does not record.
NOT_RECORDED = {W + "rPr": TEXT_REVISIONS, W + "pPr": _w("rPr", "sectPr"), W + "trPr": _w("ins", "del"),
                W + "sectPr": _w("headerReference", "footerReference")}
MOVE_RANGES = _w("moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd")
NOT_UNDONE = _w("cellIns", "cellDel", "cellMerge", "numberingChange")
REVISION_TAGS = TEXT_REVISIONS | frozenset(PROPERTY_CHANGES) | MOVE_RANGES | NOT_UNDONE
BETWEEN_PARAGRAPHS = _w("bookmarkStart", "bookmarkEnd", "commentRangeStart", "commentRangeEnd",
                        "permStart", "permEnd", "proofErr") | MOVE_RANGES
COMMENT_PARTS = {"word/comments.xml": (W + "comment", W + "id"),
                 "word/commentsExtended.xml": (W15 + "commentEx", W15 + "paraId"),
                 "word/commentsIds.xml": (W16CID + "commentId", W16CID + "paraId"),
                 "word/commentsExtensible.xml": (W16CEX + "commentExtensible", W16CEX + "durableId"),
                 "word/people.xml": (W15 + "person", W15 + "author")}
RSIDS = frozenset(W + name for name in ("rsidR", "rsidRPr", "rsidRDefault", "rsidP", "rsidDel", "rsidSect", "rsidTr"))
LAYOUT_IDS = RSIDS | {W14 + "paraId", W14 + "textId"}
IGNORED = _w("proofErr", "lastRenderedPageBreak")
PROPERTIES = _w("rPr", "pPr", "tcPr", "trPr", "tblPr", "tblPrEx", "sectPr")
RUN_WRAPPERS = TEXT_REVISIONS | _w("hyperlink", "smartTag", "customXml", "sdt", "sdtContent", "fldSimple", "dir", "bdo")
TEXT = _w("t", "delText", "instrText", "delInstrText")
LOCATED = (W + "p", W + "tblPr", W + "tblGrid", W + "trPr", W + "tcPr", W + "tblPrEx", W + "sectPr")
FINDINGS = ("authors", "dates", "ids", "structure", "placement", "existing", "untracked", "not_undone", "quality", "origins")


class CheckLimit(Exception):
    """A resource limit of this checker, not evidence of document damage."""


def _package(path: Path) -> tuple[dict[str, bytes], dict[str, ET._Element]]:
    with ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 5000 or sum(i.file_size for i in infos) > 64 * 1024 * 1024:
            raise CheckLimit("This checker supports up to 64 MiB expanded and 5,000 ZIP members. "
                             "Editing is still available; preservation was not checked.")
        if len({i.filename for i in infos}) != len(infos):
            raise ValueError("Duplicate ZIP members.")
        parts = {i.filename: archive.read(i) for i in infos if not i.is_dir()}
    if "word/document.xml" not in parts or "[Content_Types].xml" not in parts:
        raise ValueError("Not a DOCX package.")
    roots = {}
    for name, data in parts.items():
        if name.endswith((".xml", ".rels")):
            parser = ET.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
            root = ET.fromstring(data, parser)
            if root.getroottree().docinfo.doctype:
                raise ValueError("Document type declarations are not supported by this checker.")
            roots[name] = root
    return parts, roots


def _is_story(name):
    return name in {"word/document.xml", "word/footnotes.xml", "word/endnotes.xml"} or (
        name.startswith(("word/header", "word/footer")) and name.endswith(".xml"))


# ---------------------------------------------------------------- comparable form

def _attrs(node, drop, extra=()):
    return tuple(sorted((k, v) for k, v in node.attrib.items() if k not in drop and k not in extra))


def _kept(node, drop):
    if not isinstance(node.tag, str) or node.tag in IGNORED:
        return False
    if node.tag in PROPERTIES:  # an empty property container, however nested, equals no container
        return bool(_attrs(node, drop)) or any(_kept(c, drop) for c in node)
    return True


def _text(node):
    """Word text (w:t and similar) as Word reads it: without xml:space="preserve" Word drops
    leading and trailing whitespace (checked in Word 16.95)."""
    text = node.text or ""
    return text if node.get(XML_SPACE) == "preserve" else text.strip(" \t\r\n")


def _sig(node, drop=LAYOUT_IDS):
    """Property order, run boundaries, revision/layout ids and indentation do not matter; everything else does,
    including the text of any element (formulas, embedded data) and paragraph ids that comments link to."""
    if node.tag == W + "p":
        props = tuple(_sig(c, drop) for c in node if c.tag == W + "pPr" and _kept(c, drop))
        return ("p", _attrs(node, drop), props, _merge(_raw_tokens(node, drop)))
    if node.tag in TEXT:
        return node.tag, _attrs(node, drop, (XML_SPACE,)), _text(node)
    # Any other text counts exactly. Whitespace-only text between child elements is indentation,
    # unless the element also holds real text (mixed content).
    indent = len(node) > 0 and not ((node.text or "").strip() or any((c.tail or "").strip() for c in node))
    kids = [(_sig(c, drop), "" if indent else c.tail or "") for c in node if _kept(c, drop)]
    if node.tag in PROPERTIES:
        kids.sort(key=repr)
    # A split run needs a fresh formatting-revision id; its properties and coverage still count.
    extra = (W + "id",) if node.tag == W + "rPrChange" else ()
    return node.tag, _attrs(node, drop, extra), "" if indent else node.text or "", tuple(kids)


def _run_tokens(run, context, drop):
    props = run.find(W + "rPr")
    style = _sig(props, drop) if props is not None and _kept(props, drop) else None
    for item in run:
        if not _kept(item, drop) or item.tag == W + "rPr":
            continue
        if item.tag in TEXT:
            if _text(item):
                yield context, style, item.tag, _text(item)
        else:
            yield context, style, None, _sig(item, drop)


def _raw_tokens(container, drop):
    tokens = []

    def walk(node, context):
        for child in node:
            if not _kept(child, drop) or child.tag == W + "pPr":
                continue
            if child.tag == W + "r":
                tokens.extend(_run_tokens(child, context, drop))
            elif child.tag in RUN_WRAPPERS:
                # Revision ids are bookkeeping: Word may split one revision into several.
                extra = (W + "id",) if child.tag in TEXT_REVISIONS else ()
                walk(child, context + ((child.tag, _attrs(child, drop, extra)),))
            else:
                tokens.append((context, None, None, _sig(child, drop)))

    walk(container, ())
    return tokens


def _merge(tokens):
    merged = []
    for token in tokens:
        if merged and token[2] is not None and merged[-1][:3] == token[:3]:
            merged[-1] = merged[-1][:3] + (merged[-1][3] + token[3],)
        else:
            merged.append(token)
    return tuple(merged)


# ---------------------------------------------------------------- undoing new changes

def _key(node):
    return node.tag, node.get(W + "id")


def _undo(root, new, new_comment_ids):
    """Copy of a story part with new revisions and new comment anchors undone."""
    root = deepcopy(root)
    problems = []

    def fresh(tags):
        return [e for e in root.iter(*tags) if _key(e) in new]

    for anchor in list(root.iter(W + "commentRangeStart", W + "commentRangeEnd", W + "commentReference")):
        if anchor.get(W + "id") not in new_comment_ids:
            continue
        parent = anchor.getparent()
        if anchor.tag == W + "commentReference" and parent.tag == W + "r" and all(
                c is anchor or c.tag == W + "rPr" for c in parent):
            parent.getparent().remove(parent)
        else:
            parent.remove(anchor)
    for mark in fresh(_w("ins", "del")):  # inserted rows go, deleted-row marks are withdrawn
        if mark.getparent().tag == W + "trPr":
            row = mark.getparent().getparent()
            if mark.tag == W + "ins":
                row.getparent().remove(row)
            else:
                mark.getparent().remove(mark)
    for change in fresh(PROPERTY_CHANGES):  # formatting returns to the recorded properties
        container, previous = change.getparent(), change.find(PROPERTY_CHANGES[change.tag])
        if container.tag != PROPERTY_CHANGES[change.tag] or previous is None:
            problems.append({"id": change.get(W + "id"),
                             "issue": f"{_short(change.tag)} without recorded previous properties"})
            continue
        kept = [c for c in container if c is not change and c.tag in NOT_RECORDED.get(container.tag, ())]
        for child in list(container):
            container.remove(child)
        for child in kept + [deepcopy(c) for c in previous]:
            container.append(child)
    for revision in reversed(fresh(TEXT_REVISIONS | MOVE_RANGES)):
        parent = revision.getparent()
        if parent.tag == W + "rPr":
            continue  # paragraph marks below
        if revision.tag in (W + "del", W + "moveFrom"):  # the text was there before
            if next(revision.iterancestors(W + "del", W + "moveFrom"), None) is None:  # else it stays deleted text
                for node in revision.iter(W + "delText", W + "delInstrText"):
                    node.tag = W + "t" if node.tag == W + "delText" else W + "instrText"
            position = parent.index(revision)
            for child in list(revision):
                parent.insert(position, child)
                position += 1
        parent.remove(revision)
    for mark in reversed(fresh(TEXT_REVISIONS)):  # paragraph marks
        holder = mark.getparent()
        if holder.getparent() is None or holder.getparent().tag != W + "pPr":
            problems.append({"id": mark.get(W + "id"), "issue": "revision in an unsupported position"})
            continue
        paragraph = holder.getparent().getparent()
        holder.remove(mark)
        if mark.tag in (W + "del", W + "moveFrom"):
            continue
        following = paragraph.getnext()
        while following is not None and following.tag in BETWEEN_PARAGRAPHS:
            following = following.getnext()
        if following is None or following.tag != W + "p":
            problems.append({"id": mark.get(W + "id"),
                             "issue": "inserted paragraph mark has no following paragraph to merge with"})
            continue
        position = 1 if len(following) and following[0].tag == W + "pPr" else 0
        for child in [c for c in paragraph if c.tag != W + "pPr"]:
            following.insert(position, child)
            position += 1
        paragraph.getparent().remove(paragraph)
    for revision in fresh(NOT_UNDONE):
        problems.append({"id": revision.get(W + "id"), "issue": f"{_short(revision.tag)} is not undone by this checker"})
    return root, problems


def _history(root):
    """Existing revisions: text by kind/author/date (split-insensitive), marks, formatting records."""
    text, marks, formatting, formatted_runs = {}, Counter(), {}, {}
    for e in root.iter(*TEXT_REVISIONS):
        group = (_short(e.tag), e.get(W + "author"), e.get(W + "date"))
        if e.getparent().tag in (W + "rPr", W + "trPr"):
            marks[group + (_short(e.getparent().getparent().tag),)] += 1
        else:
            text.setdefault(group, []).extend(_raw_tokens(e, LAYOUT_IDS))
    for e in root.iter(*PROPERTY_CHANGES):
        run = e.getparent().getparent()
        tokens = list(_run_tokens(run, (), LAYOUT_IDS)) if (
            e.tag == W + "rPrChange" and run is not None and run.tag == W + "r") else []
        if tokens:
            group = (_short(e.tag), e.get(W + "author"), e.get(W + "date"))
            formatted_runs.setdefault(group, []).extend(tokens)
        else:
            # Empty runs have no coverage to align; retain their recorded history explicitly.
            formatting.setdefault((_short(e.tag), e.get(W + "id")), set()).add(_sig(e))
    formatting.update({k: _merge(v) for k, v in formatted_runs.items()})
    return {k: _merge(v) for k, v in text.items()}, marks, formatting


# ---------------------------------------------------------------- locating differences

def _where(node, tables):
    table = next(node.iterancestors(W + "tbl"), None)
    if table is None:
        return ""
    place = [f"table {tables.get(table, '?')}"]
    row, cell = next(node.iterancestors(W + "tr"), None), next(node.iterancestors(W + "tc"), None)
    rows = [c for c in table if c.tag == W + "tr"]
    if row in rows:
        place.append(f"row {rows.index(row) + 1}")
        cells = [c for c in row if c.tag == W + "tc"]
        if cell in cells:
            place.append(f"cell {cells.index(cell) + 1}")
    return " (" + ", ".join(place) + ")"


def _inventory(root):
    tables = {table: number for number, table in enumerate(root.iter(W + "tbl"), 1)}
    items, paragraphs = [], 0
    for node in root.iter(*LOCATED):
        if any(a.tag in PROPERTY_CHANGES for a in node.iterancestors()):
            continue  # previous properties belong to their change record
        if node.tag == W + "p":
            paragraphs += 1
            label = f"paragraph {paragraphs}"
        else:
            label = f"{_short(node.tag)} properties"
        text = "".join(_text(t) for t in node.iter(W + "t", M + "t"))[:80] if node.tag == W + "p" else ""
        items.append((label + _where(node, tables), _sig(node), text))
    return items


def _differences(original, restored, limit=8):
    a, b = _inventory(original), _inventory(restored)
    matcher = difflib.SequenceMatcher(None, [x[1] for x in a], [x[1] for x in b], autojunk=False)
    found = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            continue
        for offset in range(max(i2 - i1, j2 - j1)):
            old = a[i1 + offset] if i1 + offset < i2 else None
            new = b[j1 + offset] if j1 + offset < j2 else None
            entry = {"where": (old or new)[0], "change": "changed" if old and new else ("missing" if old else "extra")}
            if old and new:
                entry["aspect"] = "text" if old[2] != new[2] else "formatting or markup"
            if old and old[2]:
                entry["original_text"] = old[2]
            if new and new[2] and (not old or new[2] != old[2]):
                entry["text_after_undo"] = new[2]
            found.append(entry)
            if len(found) == limit:
                return found
    return found


# ---------------------------------------------------------------- per part checks

def _redline_quality(root, new, part):
    """Advisory only: equal edges of adjacent NEW, plain-text replacements.

    Never merge/repair XML. Nested history, moves, fields, formatting changes and
    large groups are outside this narrow observation; no finding is not approval.
    """
    def tokens(nodes):
        pieces, styles = [], []
        for node in nodes:
            for run in node:
                if run.tag != W + "r" or any(c.tag not in _w("rPr", "t", "delText") for c in run):
                    return None
                props = run.find(W + "rPr")
                if props is not None and any(e.tag in REVISION_TAGS for e in props.iter()):
                    return None
                style = _sig(props) if props is not None and _kept(props, LAYOUT_IDS) else None
                text = "".join(_text(c) for c in run if c.tag in _w("t", "delText"))
                pieces.append(text)
                styles.extend([style] * len(text))
                if len(styles) > 20_000:
                    return None
        text = "".join(pieces)
        return [(m.group(), tuple(styles[m.start():m.end()])) for m in TOKEN.finditer(text)]

    findings = []
    for number, paragraph in enumerate(root.iter(W + "p"), 1):
        group = []
        for node in [*paragraph, None]:
            if (node is not None and node.tag in _w("ins", "del") and _key(node) in new
                    and not any(e.tag in TEXT_REVISIONS for e in paragraph.iterancestors())):
                group.append(node)
                continue
            left = tokens([e for e in group if e.tag == W + "del"])
            right = tokens([e for e in group if e.tag == W + "ins"])
            if left and right:
                prefix = 0
                while prefix < min(len(left), len(right)) and left[prefix] == right[prefix]:
                    prefix += 1
                suffix = 0
                while suffix < min(len(left), len(right)) - prefix and left[-suffix-1] == right[-suffix-1]:
                    suffix += 1
                first = "".join(t[0] for t in left[:prefix])
                last = "".join(t[0] for t in left[len(left)-suffix:]) if suffix else ""
                if any(c.isalnum() for c in first + last):
                    findings.append({"part": part, "paragraph": number,
                                     "revision_ids": [e.get(W + "id") for e in group],
                                     "unchanged_prefix": first[:250], "unchanged_suffix": last[:250],
                                     "issue": "Equal text with identical run properties was deleted and reinserted. "
                                              "Review a narrower edit; deliberate moves or rewrites may be valid."})
            group = []
    return findings


def _added_insertions(original, restored, keys):
    """Propose whole insertion ids inside added text, never waive a comparison.

    Text alignment is only a hint. The caller must undo these ids and prove exact
    restoration of the entire story and history, including all non-text content.
    """
    left, right = list(original.iter(W + "p")), list(restored.iter(W + "p"))
    proposed = set()
    if not keys or len(left) != len(right):
        return proposed

    def text_spans(p):
        text, spans = "", {}
        for t in p.iter(*TEXT):
            if next(t.iterancestors(W + "p"), None) is not p or any(a.tag in PROPERTY_CHANGES for a in t.iterancestors()):
                continue
            start = len(text)
            text += _text(t)
            for ins in t.iterancestors(W + "ins"):
                if _key(ins) in keys:
                    first = spans.get(_key(ins), (start, start))[0]
                    spans[_key(ins)] = first, len(text)
        return text, spans

    for a, b in zip(left, right):
        if not any(_key(e) in keys for e in b.iter(W + "ins")):
            continue
        before, _ = text_spans(a)
        after, spans = text_spans(b)
        if max(len(before), len(after)) > 20_000:
            continue
        added = [(j, end) for kind, _, _, j, end in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
                 if kind == "insert"]
        proposed.update(key for key, (start, end) in spans.items()
                        if start < end and any(j <= start and end <= stop for j, stop in added))
    return proposed


def _date_issue(value, checked_at):
    """Advisory timestamp check; never normalize dates or infer a negotiation round."""
    if not value:
        return "Missing date."
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if "T" not in value or stamp.tzinfo is None:
            return "Use a date and time with an explicit timezone (normally UTC/Z)."
    except ValueError:
        return "Invalid date/time."
    if stamp > checked_at + timedelta(minutes=5):
        return "More than 5 minutes in the future at this check; verify the clock and timezone."
    return None


def _story(part, a, b, author, new_comment_ids, checked_at):
    found = {name: [] for name in FINDINGS}
    old_counts = Counter(_key(e) for e in a.iter(*REVISION_TAGS))
    old_keys = set(old_counts)
    groups = {(e.tag, e.get(W + "author"), e.get(W + "date")) for e in a.iter(*TEXT_REVISIONS)}
    old_run_properties = {_sig(e) for e in a.iter(W + "rPrChange")}
    revisions = list(b.iter(*REVISION_TAGS))
    counts = Counter(_key(e) for e in revisions)
    # Splitting an old revision still creates elements requiring valid ids.
    # Do not attribute unchanged pre-existing id defects to this task.
    for key, count in counts.items():
        if (not key[1] and key not in old_keys) or count > max(1, old_counts[key]):
            found["ids"].append({"part": part, "id": key[1], "kind": _short(key[0]),
                                 "issue": "missing id" if not key[1] else "id used more than once"})
    new, split, seen = set(), 0, set()
    same_stamp = set()
    for e in revisions:
        key = _key(e)
        if key in old_keys or key in new:
            continue
        if e.tag == W + "rPrChange" and _sig(e) in old_run_properties:
            split += 1  # Same recorded properties; coverage is checked after undoing new edits.
            continue
        if (e.tag in TEXT_REVISIONS
                and (e.tag, e.get(W + "author"), e.get(W + "date")) in groups):
            split += 1  # a piece of an existing revision; its text is still compared below
            if e.get(W + "author") == author:
                same_stamp.add(key)
            continue
        new.add(key)
    if same_stamp:
        trial, _ = _undo(b, new, new_comment_ids)
        if _sig(a) != _sig(trial):
            for extra in (same_stamp, _added_insertions(a, trial, same_stamp)):
                if not extra:
                    continue
                alternative, problems = _undo(b, new | extra, new_comment_ids)
                if problems or _sig(a) != _sig(alternative) or _history(a) != _history(alternative):
                    continue
                new |= extra
                split -= sum(_key(e) in extra for e in revisions)
                found["origins"].append({"part": part, "ids": sorted(k[1] or "" for k in extra),
                    "issue": "Old/new author and date coincide. Treating these as new exactly restores the snapshot, "
                             "but their historical provenance cannot be determined from metadata alone."})
                break
            else:
                found["origins"].append({"part": part, "ids": sorted(k[1] or "" for k in same_stamp),
                    "issue": "Old/new metadata is ambiguous and exact restoration was not established. "
                             "All differences below remain reported; no paragraph or revision was excluded."})
    for e in revisions:
        key = _key(e)
        if key not in new or key in seen:
            continue
        seen.add(key)
        where = {"part": part, "id": key[1], "kind": _short(e.tag)}
        if e.tag in (W + "moveFromRangeEnd", W + "moveToRangeEnd"):
            continue
        if e.get(W + "author") != author:
            found["authors"].append({**where, "author": e.get(W + "author")})
        date = e.get(W + "date")
        if issue := _date_issue(date, checked_at):
            found["dates"].append({**where, "date": date, "issue": issue})
        if e.tag in (W + "ins", W + "del") and e.getparent().tag not in (W + "rPr", W + "trPr"):
            outer = next(e.iterancestors(*TEXT_REVISIONS), None)
            inside = outer is not None and {
                "kind": _short(outer.tag), "id": outer.get(W + "id"), "author": outer.get(W + "author")}
            if e.tag == W + "ins" and outer is not None and outer.tag == W + "del":
                found["structure"].append({**where, "inside": inside, "issue": "new insertion inside a deletion"})
            elif outer is not None and (e.tag == W + "ins" or outer.tag in (W + "del", W + "moveFrom") or _key(outer) in new):
                found["placement"].append({**where, "inside": inside})
            wrong = (W + "delText", W + "delInstrText") if e.tag == W + "ins" else (W + "t", W + "instrText")
            if any(t.text and next(t.iterancestors(*TEXT_REVISIONS)) is e for t in e.iter(*wrong)):
                found["structure"].append({**where, "issue": "inserted text must use w:t" if e.tag == W + "ins"
                                           else "deleted text must use w:delText"})
    found["quality"] = _redline_quality(b, new, part)
    restored, problems = _undo(b, new, new_comment_ids)
    found["not_undone"] = [{"part": part, **p} for p in problems]
    old_text, old_marks, old_formatting = _history(a)
    new_text, new_marks, new_formatting = _history(restored)
    for group in sorted(set(old_text) | set(new_text), key=repr):
        if old_text.get(group) != new_text.get(group):
            found["existing"].append({"part": part, "kind": group[0], "author": group[1], "date": group[2],
                                      "issue": "text of existing revisions changed, added or removed"})
    for group in sorted(set(old_marks) | set(new_marks), key=repr):
        if old_marks.get(group, 0) != new_marks.get(group, 0):
            found["existing"].append({"part": part, "kind": group[0], "author": group[1], "date": group[2],
                                      "issue": f"existing {group[3]} revision marks changed"})
    for key in sorted(set(old_formatting) | set(new_formatting), key=repr):
        if old_formatting.get(key) != new_formatting.get(key):
            identity = {"author": key[1], "date": key[2]} if len(key) == 3 else {"id": key[1]}
            found["existing"].append({"part": part, "kind": key[0], **identity,
                                      "issue": "existing formatting revision changed or removed"})
    if _sig(a) != _sig(restored):
        found["untracked"].append({"part": part, "differences": _differences(a, restored)
                                   or [{"where": "outside paragraphs and table properties"}]})
    found["inherited"] = sum(1 for t in b.iter(W + "t") if t.text and any(
        x.tag == W + "del" and _key(x) not in new for x in t.iterancestors()))
    found["new"], found["split"] = len(new), split
    return found


def _entries(root, tag, key):
    return {} if root is None else {e.get(key): e for e in root.iter(tag)}


def _repeated(root, tag, key):
    """Keys used by more than one entry: a dictionary of entries would silently keep only one."""
    counts = Counter(e.get(key) for e in root.iter(tag)) if root is not None else Counter()
    return {k for k, n in counts.items() if n > 1}


def _package_entries(root):
    return {(e.tag, tuple(sorted(e.attrib.items()))) for e in root if isinstance(e.tag, str)}


def _other_children(root, skipped):
    return Counter(_sig(c) for c in root if isinstance(c.tag, str) and c.tag not in skipped)


def _package_issues(before, after, left, right):
    issues, metadata = [], []
    removed = sorted(set(before) - set(after))
    if removed:
        issues.append({"issue": "parts removed", "parts": removed})
    added = sorted(n for n in set(after) - set(before) if n not in COMMENT_PARTS and not n.endswith(".rels"))
    if added:
        issues.append({"issue": "unexpected new parts", "parts": added})
    for name in sorted(set(before) & set(after)):
        if before[name] == after[name] or _is_story(name) or name in COMMENT_PARTS:
            continue
        a, b = left.get(name), right.get(name)
        if name.startswith("docProps/"):
            metadata.append(name)
        elif a is None or b is None:
            issues.append({"issue": "binary part changed", "part": name})
        elif name == "[Content_Types].xml" or name.endswith(".rels"):
            if _package_entries(a) - _package_entries(b):
                issues.append({"issue": "existing package entries removed or changed", "part": name})
        elif name == "word/styles.xml":
            old, new = _entries(a, W + "style", W + "styleId"), _entries(b, W + "style", W + "styleId")
            changed = sorted(k for k, e in old.items() if k not in new or _sig(e) != _sig(new[k]))
            repeated = sorted(_repeated(b, W + "style", W + "styleId") - _repeated(a, W + "style", W + "styleId"))
            if changed or repeated or _other_children(a, {W + "style"}) != _other_children(b, {W + "style"}):
                issues.append({"issue": "existing styles changed (new styles are allowed)", "part": name,
                               "styles": changed[:20], "repeated_ids": repeated[:20]})
        elif name == "word/settings.xml":
            skipped = {W + "rsids", W + "trackRevisions"}
            if _other_children(a, skipped) != _other_children(b, skipped):
                issues.append({"issue": "settings changed beyond revision ids and the track-changes switch",
                               "part": name})
        elif name.startswith("word/") and _sig(a) != _sig(b):
            issues.append({"issue": "content changed", "part": name})
        elif not name.startswith("word/") and ET.tostring(a, method="c14n") != ET.tostring(b, method="c14n"):
            # Custom data outside word/: every character counts, including whitespace.
            issues.append({"issue": "content changed (custom data, compared exactly)", "part": name})
    return issues, metadata


def check_docx(original: Path, candidate: Path, author: str, changes_start: int = 0) -> dict:
    checks = []
    checked_at = datetime.now(timezone.utc)
    evidence = {"checked_at_utc": checked_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "native_word": "not_checked_by_server"}

    def add(name, ok, detail, problem="observation"):
        if ok:
            # Keep evidence/counters, but do not present hypothetical warnings on success.
            detail = ({key: value for key, value in detail.items()
                       if key not in ("note", "scope") and value not in ([], {})}
                      if isinstance(detail, dict) else None)
        item = {"check": name, "status": "passed" if ok else problem}
        if detail:
            item["detail"] = detail
        checks.append(item)

    try:
        before, left = _package(original)
        after, right = _package(candidate)
    except CheckLimit as exc:
        return {**evidence, "status": "not_checked", "summary": {"errors": [], "observations": [], "warnings": [],
                "not_checked": ["docx_preservation"]},
                "checks": [{"check": "docx_preservation", "status": "not_checked", "detail": str(exc)}]}
    except (OSError, ValueError, ET.XMLSyntaxError, BadZipFile, RuntimeError) as exc:
        return {**evidence, "status": "error", "summary": {"errors": ["package_and_xml"], "observations": [], "warnings": [], "not_checked": []},
                "checks": [{"check": "package_and_xml", "status": "error", "detail": str(exc)}]}
    add("package_and_xml", True, "ZIP members readable; XML well-formed. This is not an XSD or Word-opening check.")
    changed = sorted(n for n in before.keys() | after.keys() if before.get(n) != after.get(n))

    old_comments = _entries(left.get("word/comments.xml"), W + "comment", W + "id")
    new_comments = {k: e for k, e in _entries(right.get("word/comments.xml"), W + "comment", W + "id").items()
                    if k not in old_comments}
    totals = {name: [] for name in FINDINGS}
    new_count = split = inherited = 0
    for part in sorted(n for n in left.keys() | right.keys() if _is_story(n)):
        a, b = left.get(part), right.get(part)
        if a is None or b is None:
            totals["untracked"].append({"part": part, "differences": [{"where": "whole part", "change": "added or removed"}]})
            continue
        found = _story(part, a, b, author, set(new_comments), checked_at)
        for name in FINDINGS:
            totals[name].extend(found[name])
        new_count, split, inherited = new_count + found["new"], split + found["split"], inherited + found["inherited"]

    comment_issues, repeated = [], []
    for part, (tag, key) in COMMENT_PARTS.items():
        old, new = _entries(left.get(part), tag, key), _entries(right.get(part), tag, key)
        for name, entry in old.items():
            if name not in new or _sig(entry, RSIDS) != _sig(new[name], RSIDS):
                comment_issues.append({"part": part, "entry": name, "issue": "removed" if name not in new else "changed"})
        repeated += [{"part": part, "id": k, "issue": "id used by more than one entry; only one of them can be linked"}
                     for k in sorted(_repeated(right.get(part), tag, key) - _repeated(left.get(part), tag, key))]
    referenced = {e.get(W + "id") for n, root in right.items() if _is_story(n) for e in root.iter(W + "commentReference")}
    new_comment_issues = (repeated + [{"id": k, "issue": "unexpected author", "author": e.get(W + "author")}
                                      for k, e in new_comments.items() if e.get(W + "author") != author]
                          + [{"id": k, "issue": "not anchored in the text"} for k in new_comments if k not in referenced])
    comment_dates = [{"id": k, "date": e.get(W + "date"), "issue": issue}
                     for k, e in new_comments.items() if (issue := _date_issue(e.get(W + "date"), checked_at))]
    package_issues, metadata = _package_issues(before, after, left, right)

    add("package_parts", not package_issues, {"issues": package_issues, "metadata_changed": metadata})
    add("new_revision_authors", not totals["authors"],
        {"new_revisions": new_count, "unexpected_authors": totals["authors"][:20]})
    add("new_revision_dates", not totals["dates"],
        {"issues": totals["dates"][:20], "count": len(totals["dates"])} if totals["dates"] else None, "warning")
    add("new_comment_dates", not comment_dates,
        {"issues": comment_dates[:20], "count": len(comment_dates)} if comment_dates else None, "warning")
    add("new_revision_ids", not totals["ids"], totals["ids"][:20])
    add("new_revision_structure", not totals["structure"], {
        "issues": totals["structure"][:20],
        "note": "Word 16.95 opens a file with these structures only as a repaired copy."})
    add("new_revision_placement", not totals["placement"], {
        "placements": totals["placement"][:20],
        "note": "Compatibility warning, not damage. Word writes a new insertion beside other revisions, not "
                "inside them. Word 16.95 opens the tested forms (an insertion inside another author's insertion, "
                "a deletion inside a deletion) and accepts or rejects them as expected. Other editors and "
                "checkers were not tested."}, "warning")
    add("redline_quality", not totals["quality"], {
        "findings": totals["quality"][:20], "count": len(totals["quality"]),
        "scope": "Advisory comparison of equal word edges in adjacent new plain-text revisions, "
                 "with identical run properties, up to 20,000 characters per side. "
                 "Not a complete minimality check; nested history, moves, fields and complex formatting are skipped. "
                 "No document changes or save blocking."}, "warning")
    add("existing_revisions_preserved", not totals["existing"], {
        "changed_or_removed": totals["existing"][:20], "count": len(totals["existing"]),
        "split_pieces_of_existing_revisions": split,
        "scope": "All revision kinds after undoing new changes; splitting any author's earlier revision "
                 "is allowed when its text, author, date and context are kept. Origins are inferred from the snapshot; "
                 "a mismatch does not by itself prove damage when old/new metadata is ambiguous."})
    add("existing_comments_preserved", not comment_issues, {
        "changed_or_removed": comment_issues[:20],
        "scope": "Comment text, authors, dates, threads and resolved flags; anchors are checked by untracked_changes."})
    add("new_comments", not new_comment_issues, {"new_comments": len(new_comments), "issues": new_comment_issues})
    add("untracked_changes", not totals["untracked"], {
        "parts": totals["untracked"], "not_undone": totals["not_undone"],
        "scope": "Undo new revisions and new comments, then compare the whole story XML with the original: "
                 "text, formatting, paragraphs, table properties, bookmarks and existing comment anchors. "
                 "Formatting of newly inserted paragraphs is not compared."})
    if inherited:
        checks.append({"check": "inherited_deletion_text", "status": "not_checked",
                       "detail": {"ordinary_text_nodes_inside_existing_deletions": inherited,
                                  "note": "Pre-existing deletion structure needs review; no automatic repair performed."}})
    if totals["origins"]:
        checks.append({"check": "revision_origins", "status": "not_checked", "detail": totals["origins"]})
    checks.append({"check": "layout_and_legal_intent", "status": "not_checked",
                   "detail": "Native Word opening/accept-reject, XSD/relationship validation, layout, fields and legal meaning are not checked."})
    group = {s: [c["check"] for c in checks if c["status"] == s] for s in ("observation", "warning", "not_checked")}
    from veqtor_workspace.reading import change_summary, preview_guidance

    return {**evidence, "status": "diagnostic",
            "summary": {"errors": [], "observations": group["observation"], "warnings": group["warning"], "not_checked": group["not_checked"]},
            "checks": checks, "changed_parts": changed,
            "original_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
            "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "changes": change_summary(left, right, changes_start),
            "preview_guidance": preview_guidance(right, left)}
