# SPDX-License-Identifier: Apache-2.0
"""Read-only DOCX evidence. Pending markup is not a negotiation-round log.

One optional helper for model-written Python; no file writes or new MCP tool.
Paragraph/table locations refer to the package, not Word pages or logical grid cells.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from collections import Counter
from functools import cache
from itertools import zip_longest
from pathlib import Path

from veqtor_workspace.checks import (
    DOCUMENT,
    PROPERTY_CHANGES,
    REVISION_TAGS,
    TEXT_REVISIONS,
    W14,
    W15,
    M,
    W,
    _package,
    _part_pairs,
    _part_key,
    _short,
    _sig,
    _text,
)


_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_ALTERNATE = _MC + "AlternateContent"
_FALLBACK = _MC + "Fallback"


def _selected_children(node):
    """Read immediate children after expanding only compatibility fallbacks."""
    if node.tag == _ALTERNATE:
        node = node.find(_FALLBACK)
        if node is None:
            return
    for child in node:
        if child.tag == _ALTERNATE:
            yield from _selected_children(child)
        else:
            yield child


def _selected_child(node, tag):
    return next((child for child in _selected_children(node) if child.tag == tag), None)


def _fallback_view(roots):
    """Keep only stored fallbacks, without inferring any Word feature support."""
    omitted, coverage, uncovered = set(), [], {}
    for part, root in sorted(roots.items()):
        fallbacks = without_fallback = 0
        for alternate in root.iter(_ALTERNATE):
            if alternate in omitted:
                continue
            fallback = alternate.find(_FALLBACK)
            fallbacks += fallback is not None
            if fallback is None:
                without_fallback += 1
                uncovered.setdefault(part, []).append(alternate)
            for child in alternate:
                if child is not fallback:
                    omitted.update(child.iter())
        if fallbacks or without_fallback:
            coverage.append({"part": part, "fallbacks_used": fallbacks, "omitted_without_fallback": without_fallback})
    return omitted, coverage, uncovered


def normalized(text):
    return " ".join(text.split()).casefold()


NESTED_DELETION_WARNING = (
    "LibreOffice may show this nested deletion as retained/inserted text without strikethrough. "
    "Use the proposed text to read the clause; verify the revision display in Microsoft Word."
)


def _direct_strike(run):
    properties = _selected_child(run, W + "rPr")
    if properties is None:
        return None
    for tag, kind in (("dstrike", "double_strike"), ("strike", "strike")):
        value = _selected_child(properties, W + tag)
        if value is not None and value.get(W + "val", "true") in ("true", "on", "1"):
            return kind
    return None


def _deleted_row(row):
    properties = _selected_child(row, W + "trPr")
    return properties is not None and _selected_child(properties, W + "del") is not None


_INLINE_TEXT = {W + "tab": "\t", W + "ptab": "\t", W + "br": "\n", W + "cr": "\n",
                W + "noBreakHyphen": "\u2011", W + "softHyphen": "\u00ad"}


def _visible_segments(node, *, formatting=False):
    def walk(e, strike=None):
        if e.tag in (W + "del", W + "moveFrom", W + "pPr", W + "rPr"):
            return
        if e is not node and e.tag in (W + "p", W + "txbxContent"):
            return
        if formatting and e.tag == W + "r":
            strike = _direct_strike(e)
        if e.tag == W + "t":
            yield _text(e), strike
        elif e.tag in _INLINE_TEXT:
            yield _INLINE_TEXT[e.tag], strike
        elif e.tag in (W + "footnoteReference", W + "endnoteReference"):
            yield f"[{_short(e.tag)} {e.get(W + 'id')}]", strike
        else:
            for child in _selected_children(e):
                yield from walk(child, strike)

    if any(a.tag in (W + "del", W + "moveFrom") for a in node.iterancestors()):
        return
    if any(_deleted_row(row) for row in node.iterancestors(W + "tr")):
        return
    yield from walk(node)


def visible_text(node):
    """Project pending text without deletions or duplicate nested paragraphs.

    Fields and numbering keep the existing read limits reported by paragraphs().
    """
    return "".join(text for text, _ in _visible_segments(node))


def _text_and_strikes(node):
    pieces, spans = [], []
    offset = 0
    for text, strike in _visible_segments(node, formatting=True):
        end = offset + len(text)
        if strike and text:
            if spans and spans[-1]["end"] == offset and spans[-1]["kind"] == strike:
                spans[-1]["end"] = end
            else:
                spans.append({"start": offset, "end": end, "kind": strike})
        pieces.append(text)
        offset = end
    return "".join(pieces), spans


def _clip_strikes(spans, start, end):
    return [{**span, "start": max(span["start"], start), "end": min(span["end"], end)}
            for span in spans if span["end"] > start and span["start"] < end]


def _cell_excerpt(cell, projection, omitted):
    pieces, spans = [], []
    offset = 0
    for p in cell.iter(W + "p"):
        if p in omitted:
            continue
        if pieces:
            pieces.append(" ")
            offset += 1
        available = 100 - offset
        text, formatting = projection(p)
        text = text[:available]
        pieces.append(text)
        spans.extend({**span, "start": offset + span["start"], "end": offset + span["end"]}
                     for span in _clip_strikes(formatting, 0, available))
        offset += len(text)
        if offset == 100:
            break
    return {"text": "".join(pieces), **({"strikethrough_spans": spans} if spans else {})}


def _mark_strikes(text, spans, offset=0):
    pieces = []
    cursor = 0
    for span in spans:
        start, end = span["start"] - offset, span["end"] - offset
        tag = span["kind"].replace("_", "-")
        pieces.extend((text[cursor:start], f"[{tag}]{text[start:end]}[/{tag}]"))
        cursor = end
    return "".join(pieces) + text[cursor:]


def _revision(e):
    return {"kind": _short(e.tag), "id": e.get(W + "id"),
            "author": e.get(W + "author"), "date": e.get(W + "date")}


def _table_children(container, tag, omitted):
    # Rows and cells may sit inside Word content controls or custom XML.
    return [node for node in container.iter(W + tag)
            if node not in omitted and next(node.iterancestors(container.tag), None) is container]


def _paragraph_nodes(root, omitted):
    # Count raw XML paragraphs first so omitting choices never renumbers a locator.
    for number, paragraph in enumerate(root.iter(W + "p"), 1):
        if paragraph not in omitted and next(paragraph.iterancestors(*PROPERTY_CHANGES), None) is None:
            yield number, paragraph


def paragraphs(roots, *, omitted=None):
    """Paragraph evidence shared by read_docx and the before/after check report."""
    if omitted is None:
        omitted, _, _ = _fallback_view(roots)
    records = []
    table_children = cache(lambda container, tag: _table_children(container, tag, omitted))
    projection = cache(_text_and_strikes)
    cell_excerpt = cache(lambda cell: _cell_excerpt(cell, projection, omitted))
    for part, root in sorted(roots.items()):
        tables = {e: i for i, e in enumerate(root.iter(W + "tbl"), 1)}
        table_structure = {table: any(e.tag in (W + "cellIns", W + "cellDel", W + "cellMerge", W + "vMerge", W + "gridSpan")
                                     or (e.tag in (W + "trPr", W + "tcPr")
                                         and any(prop.tag in REVISION_TAGS for prop in _selected_children(e)))
                                     for e in table.iter() if e not in omitted) for table in tables if table not in omitted}
        for number, p in _paragraph_nodes(root, omitted):
            ancestors = list(p.iterancestors())
            location = {"part": part, "paragraph": number}
            table = next(p.iterancestors(W + "tbl"), None)
            row, cell = next(p.iterancestors(W + "tr"), None), next(p.iterancestors(W + "tc"), None)
            if table is not None:
                location["table"] = tables[table]
                rows = table_children(table, "tr")
                cells = table_children(row, "tc") if row is not None else []
                if row in rows:
                    location["row"] = rows.index(row) + 1
                if cell in cells:
                    location["cell"] = cells.index(cell) + 1
                    location["neighbor_cell_excerpts"] = [
                        {"cell": i + 1, **cell_excerpt(c)}
                        for i, c in enumerate(cells) if c is not cell and abs(i - cells.index(cell)) == 1]
            note = next((e for e in ancestors if e.tag in (W + "footnote", W + "endnote")), None)
            if note is not None:
                location["note_id"] = note.get(W + "id")
            own = [e for e in p.iter() if e not in omitted and (e is p or next(e.iterancestors(W + "p"), None) is p)]
            revisions = []
            structural_marks = [e for a in ancestors if a.tag in (W + "tr", W + "tc")
                                for props in _selected_children(a) if props.tag in (W + "trPr", W + "tcPr")
                                for e in props.iter() if e.tag in REVISION_TAGS and e not in omitted]
            for e in [*reversed(ancestors), *structural_marks, *own]:
                if e.tag not in REVISION_TAGS:
                    continue
                revisions.append({**_revision(e), "parents": [_revision(a) for a in reversed(list(e.iterancestors()))
                                                             if a.tag in TEXT_REVISIONS],
                                  "text": "".join(_text(t) for t in (p if e in ancestors else e).iter(W + "t", W + "delText")
                                                  if t not in omitted)})
            tags = {e.tag for e in own}
            warnings = []
            if any(r["kind"] in ("del", "moveFrom") and r["parents"] for r in revisions):
                warnings.append(NESTED_DELETION_WARNING)
            if tags & {W + "fldChar", W + "fldSimple", W + "instrText", W + "numPr"}:
                warnings.append("Fields use stored display text; automatic numbering/field calculation is not evaluated.")
            if tags & {W + "drawing", W + "pict", W + "sym", W + "object", M + "oMath", M + "oMathPara"}:
                warnings.append("Drawing, object, symbol or equation needs separate inspection; not fully represented as text.")
            if any(e.tag == W + "txbxContent" for e in ancestors):
                warnings.append("Text-box text is listed separately; its visual position is not determined.")
            if any(e.tag in TEXT_REVISIONS for e in p.findall(f"{W}pPr/{W}rPr/*")):
                warnings.append("Tracked paragraph boundary: this record is an XML paragraph, not the final paragraph layout.")
            if table_structure.get(table):
                warnings.append("Merged/tracked table structure: coordinates are XML row/cell positions; final grid is not evaluated.")
            text, strikes = projection(p)
            records.append({"id": f"{part}#p{number}", "location": location, "text": text,
                            "strikethrough_spans": strikes, "revisions": revisions, "warnings": warnings, "node": p})
    return records


def _paragraph_changes(before, after):
    """Align occurrences in each story; shared by excerpts and preview prioritization."""
    grouped = []
    for records in (before, after):
        by_part = {}
        for record in records:
            by_part.setdefault(record["location"]["part"], []).append(record)
        grouped.append(by_part)
    before_parts, after_parts = grouped
    for old_part, new_part in _part_pairs(before_parts, after_parts):
        left, right = before_parts.get(old_part, []), after_parts.get(new_part, [])
        matcher = difflib.SequenceMatcher(None, [_sig(r["node"]) for r in left],
                                          [_sig(r["node"]) for r in right], autojunk=False)
        for kind, i, j, k, stop in matcher.get_opcodes():
            if kind == "equal":
                # Keep matching XML occurrences aligned, even when siblings have
                # identical wording. An ancestor revision can change their text view.
                for old, new in zip(left[i:j], right[k:stop]):
                    if old["text"] != new["text"]:
                        yield old, new, "replace"
                continue
            for n in range(max(j - i, stop - k)):
                yield (left[i + n] if i + n < j else None,
                       right[k + n] if k + n < stop else None, kind)


def preview_guidance(roots, original):
    """Bounded evidence for affected candidate passages, using the existing text projection."""
    records = paragraphs(roots)
    affected = [r for r in records if NESTED_DELETION_WARNING in r["warnings"]]
    result = {"document": "candidate", "affected_paragraphs": len(affected), "fragments": []}
    if not affected:
        return result
    changed = {new["id"] for _, new, _ in _paragraph_changes(paragraphs(original), records) if new is not None}
    affected.sort(key=lambda r: r["id"] not in changed)
    result.update(
        warning=NESTED_DELETION_WARNING,
        excerpt_order="New/changed occurrences from paragraph-sequence comparison first, then retained passages. "
                      "Placement of indistinguishable copies can be ambiguous.",
        text_view="Proposed text: includes pending insertions and excludes deletions, including earlier rounds. "
                  "Read-only text projection, not a clean DOCX or Word layout; no revisions are accepted.",
        remaining_paragraphs=max(0, len(affected) - 5),
        read_more="Print read_docx(candidate, query='distinctive clause text', context=0); "
                  "if output continues, use run_python(output_cursor=...). query=None reads all passages.",
    )
    for r in affected[:5]:
        result["fragments"].append({"id": r["id"], "location": r["location"],
            "proposed_text": r["text"][:900], "text_length": len(r["text"]),
            "text_complete": len(r["text"]) <= 900,
            "projection_limits": [w for w in r["warnings"] if w != NESTED_DELETION_WARNING]})
    return result


def _para_id_key(value):
    return value.upper() if value and len(value) == 8 and all(c in "0123456789abcdefABCDEF" for c in value) else value


def _reading_view(roots, sources, stories):
    selected_comments = {part: roots[part] for role, (part, issue) in sources.items()
                         if role in ("comments", "extended") and part and not issue}
    return _fallback_view({**stories, **selected_comments})


def comment_inventory(roots, sources, stories, *, records=None, view=None):
    """Read comment facts for one package, including limits of the selected view."""
    (primary, issue), (extended, metadata_issue) = sources["comments"], sources["extended"]
    if issue:
        return {"total": None, "items": [], "issues": [{"part": primary, "issue": issue}]}
    primary_root = roots.get(primary)
    if primary_root is None:
        return {"total": 0, "items": [], "issues": []}

    if view is None:
        view = _reading_view(roots, sources, stories)
    omitted, _, uncovered = view
    issues = [{"part": extended, "issue": metadata_issue}] if metadata_issue else []
    complete = not any(next(block.iterancestors(W + "comment"), None) is None for block in uncovered.get(primary, ()))
    for part in (primary, extended):
        if uncovered.get(part):
            issues.append({"part": part, "issue": "Some comment XML has no stored fallback."})
    notes = [node for node in primary_root.iter(W + "comment") if node not in omitted]
    items = _comments(stories, notes, roots.get(extended) if not metadata_issue else None, omitted, records)
    return {"total": len(notes) if complete else None, "items": items, "issues": issues}


def _is_run_token(node):
    parent = node.getparent()
    # Fallback markup can wrap a run token without changing its Word parent.
    while parent is not None and parent.tag == _FALLBACK and parent.getparent() is not None and parent.getparent().tag == _ALTERNATE:
        parent = parent.getparent().getparent()
    return parent is not None and parent.tag == W + "r"


def _comments(roots, comments, extended, omitted, records):
    if not comments:
        return []
    by_node = {r["node"]: r["id"] for r in records} if records is not None else None
    anchors = {}
    for part, root in roots.items():
        active = {}
        locators = by_node if by_node is not None else {
            p: f"{part}#p{number}" for number, p in _paragraph_nodes(root, omitted)}
        for e in root.iter():
            if e in omitted:
                continue
            tag = e.tag
            marker = tag in (W + "commentRangeStart", W + "commentRangeEnd", W + "commentReference")
            if not marker and not active:
                continue
            p = e if tag == W + "p" else next(e.iterancestors(W + "p"), None)
            pid = locators.get(p)
            if tag == W + "p" and pid:
                for anchor in active.values():
                    anchor["text"].append("\n")
                    if pid not in anchor["paragraphs"]:
                        anchor["paragraphs"].append(pid)
            elif marker:
                cid = e.get(W + "id")
                anchor = anchors.setdefault(cid, {"paragraphs": [], "text": []})
                if pid and pid not in anchor["paragraphs"]:
                    anchor["paragraphs"].append(pid)
                if tag == W + "commentRangeStart":
                    active[cid] = anchor
                elif tag == W + "commentRangeEnd":
                    active.pop(cid, None)
            elif tag in (W + "t", W + "delText") or (tag in _INLINE_TEXT and _is_run_token(e)):
                text = _INLINE_TEXT[tag] if tag in _INLINE_TEXT else _text(e)
                for anchor in active.values():
                    if pid and pid not in anchor["paragraphs"]:
                        anchor["paragraphs"].append(pid)
                    anchor["text"].append(text)

    paragraphs = {c: [p for p in c.iter(W + "p") if p not in omitted] for c in comments}
    para_keys = {p: _para_id_key(p.get(W14 + "paraId")) for ps in paragraphs.values() for p in ps}
    counts = Counter(key for key in para_keys.values() if key)
    ids = Counter(c.get(W + "id") for c in comments)
    para_ids = {key: c.get(W + "id") for c, ps in paragraphs.items() for p in ps
                if (key := para_keys[p]) and counts[key] == 1 and ids[c.get(W + "id")] == 1}
    entries = [e for e in _selected_children(extended) if e.tag == W15 + "commentEx"] if extended is not None else []
    keys = [_para_id_key(e.get(W15 + "paraId")) for e in entries]
    metadata_counts = Counter(keys)
    metadata = {key: e for key, e in zip(keys, entries) if key and metadata_counts[key] == 1 and counts[key] == 1}
    extras = {c: next((metadata[para_keys[p]] for p in paragraphs[c] if para_keys[p] in metadata), None) for c in comments}
    parent_keys = {c: _para_id_key(ex.get(W15 + "paraIdParent")) if ex is not None else None for c, ex in extras.items()}
    parents = {c.get(W + "id"): para_ids.get(parent_keys[c]) for c in comments if ids[c.get(W + "id")] == 1}
    result = []
    for c in comments:
        cid = c.get(W + "id")
        ex, parent = extras[c], parents.get(cid)
        if ex is None:
            parent_state = "unknown"
        elif ex.get(W15 + "paraIdParent") is None:
            parent_state = "standalone"
        else:
            parent_state = "linked" if parent is not None else "unresolved"
        target, visited = cid, set()
        while target is not None and ids[target] == 1 and target not in visited:
            if anchors.get(target, {}).get("paragraphs"):
                break
            visited.add(target)
            target = parents.get(target)
        else:
            target = None
        anchor = anchors.get(target, {"paragraphs": [], "text": []})
        anchor_text = "".join(anchor["text"])
        done = ex.get(W15 + "done", "0") if ex is not None else None
        resolved = True if done in ("1", "true", "on") else False if done in ("0", "false", "off") else None
        text, strikes = _comment_text_and_strikes(paragraphs[c])
        result.append({"type": "comment", "id": cid, "author": c.get(W + "author"), "date": c.get(W + "date"),
                       "parent_comment_id": parent, "parent_status": parent_state, "resolved": resolved,
                       "paragraphs": anchor["paragraphs"], "anchor_markup": anchor_text,
                       "anchor_markup_excerpt": anchor_text[:240],
                       "anchor_status": ("ambiguous" if ids[cid] > 1 else "located" if anchor["paragraphs"] else "not_located"),
                       "text": text, **({"strikethrough_spans": strikes} if strikes else {})})
    return result


def _comment_text_and_strikes(paragraphs):
    pieces, strikes, offset = [], [], 0
    for p in paragraphs:
        text, spans = _text_and_strikes(p)
        pieces.append(text)
        strikes.extend({**span, "start": offset + span["start"], "end": offset + span["end"]} for span in spans)
        offset += len(text) + 1
    return "\n".join(pieces), strikes


def _select_table_row(records, address, omitted):
    if not isinstance(address, str) or not address:
        raise ValueError("table_row must be a paragraph address, such as p1423 or word/document.xml#p1423.")
    part, sep, paragraph = address.rpartition("#")
    part_key = _part_key(part) if sep else DOCUMENT
    target = next((record for record in records if _part_key(record["location"]["part"]) == part_key
                   and record["id"].rpartition("#")[2] == paragraph), None)
    if target is None:
        raise ValueError(f"Paragraph {address!r} was not found. Use the reader address from this document version.")
    row = next(target["node"].iterancestors(W + "tr"), None)
    location = target["location"]
    if row is None or "row" not in location or "cell" not in location:
        raise ValueError(f"Paragraph {address!r} is outside a table row.")

    row_location = {key: location[key] for key in ("part", "table", "row")}
    by_node = {record["node"]: record for record in records}
    groups = [({**row_location, "cell": number},
               [by_node[p] for p in cell.iter(W + "p") if p in by_node])
              for number, cell in enumerate(_table_children(row, "tc", omitted), 1)]
    return groups, {**row_location, "paragraph": target["id"], "cells": len(groups)}


def read_docx(path, query=None, *, details=False, context=1, format="text", table_row=None):
    """Read complete passages as compact text; format='data' returns structured evidence.

    print(read_docx(path, query='8.8', details=True, context=5)).
    table_row='p1423' reads the physical row containing that paragraph. Use the full
    paragraph ID for other document parts. table_row takes the place of query and context.
    Otherwise, query=None reads the whole document, and context selects adjacent XML
    paragraphs around matches. Matching paragraphs come first. No element, text-length or page cap:
    run_python stores the full output and supplies its single output_cursor continuation.
    Stable paragraph IDs refer to this file version. No file edits or revision acceptance.
    """
    if type(context) is not int or context < 0:
        raise ValueError("context must be a nonnegative integer.")
    if format not in ("text", "data"):
        raise ValueError("format must be 'text' or 'data'.")
    if query is not None and (not isinstance(query, str) or not normalized(query)):
        raise ValueError("query must be nonempty text or None.")
    if table_row is not None and query is not None:
        raise ValueError("Use either table_row or query.")
    path = Path(path)
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    _, roots, unread, sources, (stories, story_issues, _, unverified_stories, _, _) = _package(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != before_hash:
        raise ValueError("Document changed while reading; read the saved version again.")
    view = _reading_view(roots, sources, stories)
    omitted, alternatives, _ = view
    records = paragraphs(stories, omitted=omitted)
    inventory = comment_inventory(roots, sources, stories, records=records, view=view)
    comments = inventory["items"]
    related = {}
    for comment in comments:
        for paragraph_id in comment["paragraphs"]:
            related.setdefault(paragraph_id, []).append(comment)
    query_key = normalized(query) if query else None
    row = None
    matching_ids = set()
    if table_row is not None:
        groups, row = _select_table_row(records, table_row, omitted)
        matched = 1
    else:
        hits = [i for i, r in enumerate(records) if query_key is None or query_key in normalized("\n".join(
            [r["text"], *(x["text"] for x in r["revisions"]), *(c["text"] for c in related.get(r["id"], ()))]))]
        selected = sorted({j for i in hits for j in range(max(0, i - context), min(len(records), i + context + 1))
                           if records[j]["location"]["part"] == records[i]["location"]["part"]})
        if query_key is not None:
            matching_ids = {records[i]["id"] for i in hits}
            hit_set = set(hits)
            selected = hits + [i for i in selected if i not in hit_set]
        groups = [(None, [records[i] for i in selected])]
        matched = len(hits)
    items, used_comments = [], set()
    for cell, group in groups:
        if cell is not None:
            items.append({"type": "table_cell", "location": cell})
        for r in group:
            items.append({"type": "paragraph", "id": r["id"], "location": r["location"], "text": r["text"],
                          "revision_count": len(r["revisions"]), "warnings": r["warnings"],
                          **({"strikethrough_spans": r["strikethrough_spans"]} if r["strikethrough_spans"] else {})})
            if details:
                for revision in r["revisions"]:
                    items.append({"type": "revision", "paragraph": r["id"], **revision})
            for comment in related.get(r["id"], ()):
                if id(comment) not in used_comments:
                    items.append(comment)
                    used_comments.add(id(comment))
    if row is None:
        for comment in comments:
            if comment["anchor_status"] != "located" and (query_key is None or query_key in normalized(comment["text"])):
                items.append(comment)
    order = "table_row" if row is not None else ("matches_then_context" if query_key is not None else "document")
    result = {"sha256": before_hash, "view": "text_with_pending_changes_incorporated_not_approval",
              "order": order,
              "scope": "Body, tables, headers/footers, footnotes/endnotes and located comments. Retained markup only; "
                       "accepted/deleted history cannot be reconstructed. Numbering, fields, drawings and final layout are not evaluated.",
              "paragraphs": len(records), "matched_paragraphs": matched, "comments": inventory["total"],
              "unread_embedded_parts": sorted(n for n, root in stories.items()
                                             if any(e not in omitted for e in root.iter(W + "altChunk"))),
              "items": items}
    if inventory["issues"]:
        result["comment_issues"] = inventory["issues"]
    if row is not None:
        result["table_row"] = row
    if story_issues:
        result["unread_story_parts"] = story_issues
    if unverified_stories:
        result["unverified_story_parts"] = sorted(unverified_stories)
    if auxiliary := {part: details["issue"] for part, details in unread.items() if part not in story_issues}:
        result["unread_auxiliary_xml"] = auxiliary
    if alternatives:
        result["alternate_content"] = alternatives
    return _compact(result, matching_ids) if format == "text" else result


def _compact(result, matching_ids):
    """Display evidence without repeating field names, warning text or cell locations."""
    parts = sorted({x["location"]["part"] for x in result["items"] if x["type"] == "paragraph"})
    aliases = {p: ("" if p == "word/document.xml" else f"s{i}:") for i, p in enumerate(parts, 1)}

    def label(pid):
        part, _, number = pid.rpartition("#p")
        return f"{aliases.get(part, part + '#')}p{number}"

    def ranges(ids):
        grouped = {}
        for pid in ids:
            part, _, number = pid.rpartition("#p")
            grouped.setdefault(part, set()).add(int(number))
        labels = []
        for part, numbers in grouped.items():
            groups = []
            for n in sorted(numbers):
                if groups and groups[-1][-1] + 1 == n:
                    groups[-1].append(n)
                else:
                    groups.append([n])
            labels.extend(label(f"{part}#p{g[0]}") + (f"-{label(f'{part}#p{g[-1]}')}" if len(g) > 1 else "")
                          for g in groups)
        return ",".join(labels) or "unlocated"

    lines = ["DOCX text with pending insertions included and deletions excluded; no revisions accepted.",
             f"SHA256 {result['sha256']}; order={result['order']}; matches={result['matched_paragraphs']}",
             "Locations use XML paragraphs/table rows/cells, not Word pages or final grid coordinates."]
    if any(x.get("revision_count", 0) for x in result["items"]):
        lines.append("[rN] counts retained revision marks, not negotiation rounds; details=True shows history.")
    lines.extend(f"{prefix}pN = {part}#pN" for part, prefix in aliases.items())
    if any(item.get("strikethrough_spans") for item in result["items"]):
        lines.append("[strike] and [double-strike] mark direct Word formatting.")
    if row := result.get("table_row"):
        lines.append(f"Physical table row containing {label(row['paragraph'])}.")
    warnings, last_location, row_hint = {}, None, None
    for item in result["items"]:
        if item["type"] in ("paragraph", "table_cell"):
            loc = item["location"]
            location = tuple((k, loc[k]) for k in ("part", "table", "row", "cell", "note_id") if k in loc)
            if item["type"] == "table_cell" or location != last_location:
                short = {"table": "t", "row": "r", "cell": "c", "note_id": "note"}
                lines.append("[" + (aliases.get(loc["part"]) or "body") + " "
                             + " ".join(f"{short[k]}{v}" for k, v in location if k != "part") + "]")
                last_location = location
                if result["order"] == "matches_then_context" and (excerpts := loc.get("neighbor_cell_excerpts")):
                    for excerpt in excerpts:
                        text = _mark_strikes(excerpt["text"], excerpt.get("strikethrough_spans", ()))
                        lines.append(f"  c{excerpt['cell']} excerpt (up to 100 characters): {text}")
            if item["type"] == "table_cell":
                continue
            if row_hint is None and "cell" in loc and item["id"] in matching_ids:
                row_hint = label(item["id"]) if loc["part"] == "word/document.xml" else item["id"]
            revision_mark = f"[r{item['revision_count']}] " if item["revision_count"] else ""
            text = _mark_strikes(item["text"], item.get("strikethrough_spans", ()))
            lines.append(f"{label(item['id'])} {revision_mark}{text}")
            for warning in item["warnings"]:
                warnings.setdefault(warning, []).append(item["id"])
        elif item["type"] == "revision":
            meta = {k: item[k] for k in ("kind", "id", "author", "date", "parents")}
            lines.append("  revision " + json.dumps(meta, ensure_ascii=False, separators=(",", ":")) + " " + item["text"])
        else:
            meta = {k: item[k] for k in ("id", "author", "date", "parent_comment_id", "parent_status", "resolved") if item.get(k) is not None}
            lines.append("  comment " + json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
                         + f" at {ranges(item['paragraphs'])}: "
                         + _mark_strikes(item["text"], item.get("strikethrough_spans", ())))
            if item["anchor_markup_excerpt"]:
                lines.append("    anchor excerpt: " + item["anchor_markup_excerpt"])
    if row_hint:
        lines.append(f"Read all cells in a hit's row with read_docx(path, table_row={row_hint!r}).")
    for entry in result.get("alternate_content", ()):
        lines.append(f"Word compatibility in {entry['part']}: stored fallbacks used: {entry['fallbacks_used']}; "
                     f"omitted without a fallback: {entry['omitted_without_fallback']}. Word may choose a different branch.")
    for issue in result.get("comment_issues", ()):
        lines.append(f"Comment facts limited ({issue['part']}): {issue['issue']}")
    for part, issue in result.get("unread_story_parts", {}).items():
        lines.append(f"Text coverage limited ({part}): {issue}")
    if unverified := result.get("unverified_story_parts"):
        lines.append("Legacy stories found by filename; document attachment unverified: " + ", ".join(unverified))
    if unread := result.get("unread_auxiliary_xml"):
        lines.append("Unparsed auxiliary XML: " + ", ".join(unread))
    if result["unread_embedded_parts"]:
        lines.append("Unexpanded embedded content: " + ", ".join(result["unread_embedded_parts"]))
    lines.append("Coverage: " + result["scope"])
    lines.extend(f"NOTE {ranges(ids)}: {warning}" for warning, ids in warnings.items())
    return "\n".join(lines)


def _first_strike_change(old, new):
    for a, b in zip_longest(old, new):
        if a == b:
            continue
        if a is None or b is None:
            return (a if b is None else b)["start"]
        if a["kind"] != b["kind"] or a["start"] != b["start"]:
            return min(a["start"], b["start"])
        return min(a["end"], b["end"])
    return 0


def change_summary(before, after, start=0):
    """Compact differences between two documents."""
    if type(start) is not int or start < 0:
        raise ValueError("changes_start must be a nonnegative integer.")
    omitted_before, original_alternatives, _ = _fallback_view(before)
    omitted_after, candidate_alternatives, _ = _fallback_view(after)
    changes = []

    def brief(r, offset=0):
        end = offset + 240
        text = r["text"][offset:end]
        spans = _clip_strikes(r["strikethrough_spans"], offset, end)
        return {"id": r["id"], "location": r["location"], "text_excerpt": text, "text_offset": offset,
                "text_length": len(r["text"]), "revision_count": len(r["revisions"]), "warnings": r["warnings"],
                **({"strikethrough_spans": spans, "formatted_excerpt": _mark_strikes(text, spans, offset)} if spans else {})}
    for old, new, kind in _paragraph_changes(paragraphs(before, omitted=omitted_before),
                                             paragraphs(after, omitted=omitted_after)):
        offset = 0
        if old and new and old["text"] != new["text"]:
            shared = next((i for i, pair in enumerate(zip(old["text"], new["text"])) if pair[0] != pair[1]),
                          min(len(old["text"]), len(new["text"])))
            offset = max(0, shared - 70)
        elif old and new:
            offset = max(0, _first_strike_change(old["strikethrough_spans"], new["strikethrough_spans"]) - 70)
        changes.append({"before": brief(old, offset) if old else None, "after": brief(new, offset) if new else None,
                        "kind": "formatting_or_markup" if old and new and old["text"] == new["text"] else kind})
    result = {"scope": "Original versus candidate, including markup changes. Excerpts show the first text difference "
                       "or the first direct strikethrough difference when wording is unchanged. "
                       "Adjacent changed paragraphs are paired by order.",
              "total": len(changes), "items": changes[start:start + 5],
              "next_start": start + 5 if start + 5 < len(changes) else None}
    if original_alternatives or candidate_alternatives:
        result["scope"] += (" Paragraph excerpts use stored Word compatibility fallbacks. Blocks without a fallback "
                            "are omitted. Changes limited to other branches may be absent from the paragraph count.")
        result["alternate_content"] = {"original": original_alternatives, "candidate": candidate_alternatives}
    return result
