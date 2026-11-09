# SPDX-License-Identifier: Apache-2.0
"""Read-only, paged DOCX evidence. Pending markup is not a negotiation-round log.

One optional helper for model-written Python; no file writes or new MCP tool.
Paragraph/table locations refer to the package, not Word pages or logical grid cells.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path

from veqtor_workspace.checks import (
    PROPERTY_CHANGES,
    REVISION_TAGS,
    TEXT_REVISIONS,
    W14,
    W15,
    M,
    W,
    _is_story,
    _package,
    _short,
    _sig,
    _text,
)


def normalized(text):
    return " ".join(text.split()).casefold()


def visible_text(node):
    """Text projection only: include insertions, exclude deletions/move sources.

    Do not concatenate nested paragraphs twice or interpret fields/numbering.
    Structural exceptions are reported by paragraphs(), not silently certified.
    """
    def walk(e):
        if e.tag in (W + "del", W + "moveFrom"):
            return ""
        if e is not node and e.tag in (W + "p", W + "txbxContent"):
            return ""
        if e.tag == W + "t":
            return _text(e)
        if e.tag in (W + "tab", W + "ptab"):
            return "\t"
        if e.tag in (W + "br", W + "cr"):
            return "\n"
        if e.tag == W + "noBreakHyphen":
            return "\u2011"
        if e.tag == W + "softHyphen":
            return "\u00ad"
        if e.tag in (W + "footnoteReference", W + "endnoteReference"):
            return f"[{_short(e.tag)} {e.get(W + 'id')}]"
        return "".join(walk(child) for child in e)

    if any(a.tag in (W + "del", W + "moveFrom") for a in node.iterancestors()):
        return ""
    if any(row.find(f"{W}trPr/{W}del") is not None for row in node.iterancestors(W + "tr")):
        return ""
    return walk(node)


def _revision(e):
    return {"kind": _short(e.tag), "id": e.get(W + "id"),
            "author": e.get(W + "author"), "date": e.get(W + "date")}


def paragraphs(roots):
    """Paragraph evidence shared by read_docx and the before/after check report."""
    records = []
    for part, root in sorted(roots.items()):
        if not _is_story(part):
            continue
        tables = {e: i for i, e in enumerate(root.iter(W + "tbl"), 1)}
        table_structure = {table: any(e.tag in (W + "cellIns", W + "cellDel", W + "cellMerge", W + "vMerge", W + "gridSpan")
                                     or (e.tag in REVISION_TAGS and e.getparent().tag in (W + "trPr", W + "tcPr"))
                                     for e in table.iter()) for table in tables}
        for number, p in enumerate(root.iter(W + "p"), 1):
            ancestors = list(p.iterancestors())
            if any(e.tag in PROPERTY_CHANGES for e in ancestors):
                continue
            location = {"part": part, "paragraph": number}
            table = next(p.iterancestors(W + "tbl"), None)
            row, cell = next(p.iterancestors(W + "tr"), None), next(p.iterancestors(W + "tc"), None)
            if table is not None:
                location["table"] = tables[table]
                rows = list(table.iterchildren(W + "tr"))
                cells = list(row.iterchildren(W + "tc")) if row is not None else []
                if row in rows:
                    location["row"] = rows.index(row) + 1
                if cell in cells:
                    location["cell"] = cells.index(cell) + 1
                    location["neighbor_cell_excerpts"] = [
                        {"cell": i + 1, "text": " ".join(visible_text(x) for x in c.iter(W + "p"))[:100]}
                        for i, c in enumerate(cells) if c is not cell and abs(i - cells.index(cell)) == 1]
            note = next((e for e in ancestors if e.tag in (W + "footnote", W + "endnote")), None)
            if note is not None:
                location["note_id"] = note.get(W + "id")
            own = [e for e in p.iter() if e is p or next(e.iterancestors(W + "p"), None) is p]
            revisions = []
            structural_marks = [e for a in ancestors if a.tag in (W + "tr", W + "tc")
                                for props in a if props.tag in (W + "trPr", W + "tcPr")
                                for e in props.iter() if e.tag in REVISION_TAGS]
            for e in [*reversed(ancestors), *structural_marks, *own]:
                if e.tag not in REVISION_TAGS:
                    continue
                revisions.append({**_revision(e), "parents": [_revision(a) for a in reversed(list(e.iterancestors()))
                                                             if a.tag in TEXT_REVISIONS],
                                  "text": "".join(_text(t) for t in (p if e in ancestors else e).iter(W + "t", W + "delText"))})
            tags = {e.tag for e in own}
            warnings = []
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
            records.append({"id": f"{part}#p{number}", "location": location, "text": visible_text(p),
                            "revisions": revisions, "warnings": warnings, "node": p})
    return records


def _comments(roots, records):
    by_node = {r["node"]: r["id"] for r in records}
    anchors = {}
    for part, root in roots.items():
        if not _is_story(part):
            continue
        active = {}
        for e in root.iter():
            p = next(e.iterancestors(W + "p"), None)
            pid = by_node.get(p)
            if e.tag in (W + "commentRangeStart", W + "commentRangeEnd", W + "commentReference"):
                cid = e.get(W + "id")
                anchor = anchors.setdefault(cid, {"paragraphs": [], "text": []})
                if pid and pid not in anchor["paragraphs"]:
                    anchor["paragraphs"].append(pid)
                if e.tag == W + "commentRangeStart":
                    active[cid] = anchor
                elif e.tag == W + "commentRangeEnd":
                    active.pop(cid, None)
            if e.tag in (W + "t", W + "delText"):
                for anchor in active.values():
                    if pid and pid not in anchor["paragraphs"]:
                        anchor["paragraphs"].append(pid)
                    anchor["text"].append(_text(e))
    root = roots.get("word/comments.xml")
    comments = list(root.iter(W + "comment")) if root is not None else []
    para_ids = {p.get(W14 + "paraId"): c.get(W + "id") for c in comments for p in c.iter(W + "p")
                if p.get(W14 + "paraId")}
    extended = roots.get("word/commentsExtended.xml")
    metadata = {e.get(W15 + "paraId"): e for e in extended} if extended is not None else {}
    extras = {c.get(W + "id"): next((metadata[p.get(W14 + "paraId")] for p in c.iter(W + "p")
                                    if p.get(W14 + "paraId") in metadata), None) for c in comments}
    parents = {cid: para_ids.get(ex.get(W15 + "paraIdParent")) if ex is not None else None
               for cid, ex in extras.items()}
    result = []
    for c in comments:
        cid = c.get(W + "id")
        ex, parent = extras[cid], parents[cid]
        target, visited = cid, set()
        while target is not None and target not in visited and not anchors.get(target, {}).get("paragraphs"):
            visited.add(target)
            target = parents.get(target)
        anchor = anchors.get(target, {"paragraphs": [], "text": []})
        result.append({"type": "comment", "id": cid, "author": c.get(W + "author"), "date": c.get(W + "date"),
                       "parent_comment_id": parent, "resolved": ex.get(W15 + "done") in ("1", "true") if ex is not None else None,
                       "paragraphs": anchor["paragraphs"], "anchor_markup_excerpt": "".join(anchor["text"])[:240],
                       "anchor_status": "located" if anchor["paragraphs"] else "not_located",
                       "text": "\n".join(visible_text(p) for p in c.iter(W + "p"))})
    return result


def _chunks(item):
    text = item["text"]
    for offset in range(0, max(len(text), 1), 900):
        yield {**item, "text": text[offset:offset + 900], "text_offset": offset,
               "text_length": len(text), "text_continues": offset + 900 < len(text)}


def read_docx(path, query=None, *, details=False, context=1, limit=6, cursor=None):
    """Read selected passages, their comments and optionally retained revision evidence.

    print(json.dumps(read_docx(path, query='8.8', details=True), ensure_ascii=False)).
    Repeat with the returned next_cursor and identical options until it is None.
    The cursor is bound to file bytes and options. Output stays below runner stdout's
    normal 12 KB reply; long text is continued, never silently cut. No edits/acceptance.
    """
    if not isinstance(limit, int) or not 1 <= limit <= 20 or not isinstance(context, int) or not 0 <= context <= 3:
        raise ValueError("Use limit 1–20 and context 0–3; read further pages with next_cursor.")
    if query is not None and (not isinstance(query, str) or not normalized(query)):
        raise ValueError("query must be nonempty text or None.")
    path = Path(path)
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    _, roots = _package(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != before_hash:
        raise ValueError("Document changed while reading; read the saved version again.")
    records = paragraphs(roots)
    comments = _comments(roots, records)
    signature = hashlib.sha256(json.dumps([before_hash, query, details, context, limit]).encode()).hexdigest()[:24]
    offset = 0
    if cursor is not None:
        stamp, sep, value = str(cursor).partition(":")
        if stamp != signature or not sep or not value.isdecimal():
            raise ValueError("Reading cursor does not match this document/options; start again without it.")
        offset = int(value)
    related = {r["id"]: [c for c in comments if r["id"] in c["paragraphs"]] for r in records}
    query_key = normalized(query) if query else None
    hits = [i for i, r in enumerate(records) if query_key is None or query_key in normalized("\n".join(
        [r["text"], *(x["text"] for x in r["revisions"]), *(c["text"] for c in related[r["id"]])]))]
    selected = sorted({j for i in hits for j in range(max(0, i - context), min(len(records), i + context + 1))
                       if records[j]["location"]["part"] == records[i]["location"]["part"]})
    items, used_comments = [], set()
    for i in selected:
        r = records[i]
        items.extend(_chunks({"type": "paragraph", "id": r["id"], "location": r["location"], "text": r["text"],
                              "revision_count": len(r["revisions"]), "warnings": r["warnings"]}))
        if details:
            for revision in r["revisions"]:
                items.extend(_chunks({"type": "revision", "paragraph": r["id"], **revision}))
        for comment in related[r["id"]]:
            if comment["id"] not in used_comments:
                items.extend(_chunks(comment))
                used_comments.add(comment["id"])
    for comment in comments:
        if comment["anchor_status"] == "not_located" and (query_key is None or query_key in normalized(comment["text"])):
            items.extend(_chunks(comment))
    result = {"sha256": before_hash, "view": "text_with_pending_changes_incorporated_not_approval",
              "scope": "Body, tables, headers/footers, footnotes/endnotes and located comments. Retained markup only; "
                       "accepted/deleted history cannot be reconstructed. Numbering, fields, drawings and final layout are not evaluated.",
              "paragraphs": len(records), "matched_paragraphs": len(hits), "comments": len(comments),
              "unread_embedded_parts": sorted(n for n in roots if _is_story(n) and any(
                  e.tag == W + "altChunk" for e in roots[n].iter())),
              "items": [], "next_cursor": None}
    if offset > len(items):
        raise ValueError("Reading cursor is outside this result.")
    for index in range(offset, min(offset + limit, len(items))):
        trial = {**result, "items": [*result["items"], items[index]], "next_cursor": f"{signature}:{index + 1}"}
        if len(json.dumps(trial).encode()) > 10_000:
            if not result["items"]:
                raise ValueError("This item has unusually large metadata; inspect this fragment with custom Python.")
            break
        result["items"].append(items[index])
    consumed = offset + len(result["items"])
    result["next_cursor"] = f"{signature}:{consumed}" if consumed < len(items) else None
    return result


def change_summary(before, after, start=0):
    """Compact current-task differences; use read_docx for full text/history."""
    if type(start) is not int or start < 0:
        raise ValueError("changes_start must be a nonnegative integer.")
    a, b = paragraphs(before), paragraphs(after)
    changes = []
    for part in sorted({r["location"]["part"] for r in a + b}):
        left, right = ([r for r in records if r["location"]["part"] == part] for records in (a, b))
        matcher = difflib.SequenceMatcher(None, [_sig(r["node"]) for r in left],
                                          [_sig(r["node"]) for r in right], autojunk=False)
        def brief(r, offset=0):
            return {"id": r["id"], "location": r["location"], "text_excerpt": r["text"][offset:offset + 240],
                    "text_offset": offset,
                    "text_length": len(r["text"]), "revision_count": len(r["revisions"]), "warnings": r["warnings"]}
        for kind, i, j, k, l in matcher.get_opcodes():
            if kind == "equal":
                continue
            for n in range(max(j - i, l - k)):
                old, new = left[i + n] if i + n < j else None, right[k + n] if k + n < l else None
                offset = 0
                if old and new and old["text"] != new["text"]:
                    shared = next((i for i, pair in enumerate(zip(old["text"], new["text"])) if pair[0] != pair[1]),
                                  min(len(old["text"]), len(new["text"])))
                    offset = max(0, shared - 70)
                changes.append({"before": brief(old, offset) if old else None, "after": brief(new, offset) if new else None,
                                "kind": "formatting_or_markup" if old and new and old["text"] == new["text"] else kind})
    return {"scope": "Snapshot before this task versus result, including markup changes; excerpts show the first text difference, not full text. "
                     "Adjacent changed paragraphs are paired by order, not semantic identity. This is not a history of rounds.",
            "total": len(changes), "items": changes[start:start + 5],
            "next_start": start + 5 if start + 5 < len(changes) else None}
