# SPDX-License-Identifier: Apache-2.0
"""Bounded observations, not a universal DOCX validator or a publication gate.

Undo new revisions and compare the story XML, excluding comment anchors from
preservation diagnostics. Paragraph excerpts retain actual markup differences.
Comment facts are read independently for each selected document.
"""

from __future__ import annotations

import difflib
import hashlib
import lzma
import os
import posixpath
import re
import zlib
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

from lxml import etree as ET

from veqtor_workspace.text_edits import REVISION_DATE_UTC, TOKEN

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
W14 = "{http://schemas.microsoft.com/office/word/2010/wordml}"
W15 = "{http://schemas.microsoft.com/office/word/2012/wordml}"
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
DOCUMENT = "word/document.xml"
MANIFEST = "[Content_Types].xml"
DOCUMENT_RELS = "word/_rels/document.xml.rels"
_OFFICE_REL = ("http://schemas.openxmlformats.org/officeDocument/2006/relationships/",
               "http://purl.oclc.org/ooxml/officeDocument/relationships/")
_RELATIONSHIP_IDS = tuple("{" + uri.rstrip("/") + "}id" for uri in _OFFICE_REL)
_STORY_ROLES = {"header": W + "hdr", "footer": W + "ftr", "footnotes": W + "footnotes", "endnotes": W + "endnotes"}
_STORY_ROLE_FOR_TAG = {tag: role for role, tag in _STORY_ROLES.items()}
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_COMMENT_SOURCES = {
    "comments": ("word/comments.xml", W + "comments", {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
        "http://purl.oclc.org/ooxml/officeDocument/relationships/comments"}),
    "extended": ("word/commentsExtended.xml", W15 + "commentsEx", {
        "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"}),
    "ids": ("word/commentsIds.xml", "{http://schemas.microsoft.com/office/word/2016/wordml/cid}commentsIds", {
        "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds"}),
    "extensible": ("word/commentsExtensible.xml", "{http://schemas.microsoft.com/office/word/2018/wordml/cex}commentsExtensible", {
        "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible"}),
    "people": ("word/people.xml", W15 + "people", {"http://schemas.microsoft.com/office/2011/relationships/people"}),
}
COMMENT_ANCHORS = _w("commentRangeStart", "commentRangeEnd", "commentReference")
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


def _xml_root(data):
    parser = ET.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = ET.fromstring(data, parser)
    if root.getroottree().docinfo.doctype:
        raise ValueError("Document type declarations are not supported by this checker.")
    return root


def _part_key(name):
    return name.translate(_ASCII_LOWER)


def _part_names(parts):
    names = {}
    for name in parts:
        key = _part_key(name)
        names[key] = None if key in names else name
    return names


def _part_pairs(before, after):
    """Keep real member names, folding case only for unique matches."""
    original, candidate = _part_names(before), _part_names(after)
    matches = {name: name for name in before.keys() & after.keys()}
    matches.update({original[key]: candidate[key] for key in original.keys() & candidate.keys()
                    if original[key] is not None and candidate[key] is not None})
    def order(name):
        return _part_key(name), name

    for name in sorted(before, key=order):
        yield name, matches.get(name)
    for name in sorted(after.keys() - matches.values(), key=order):
        yield None, name


def _part_name(names, requested):
    key = _part_key(requested)
    if key not in names:
        return requested, "The linked part is missing."
    if names[key] is None:
        return requested, "More than one archive member matches the part name."
    return names[key], None


def _relationship_target(link):
    raw = link.get("Target", "")
    try:
        target = urlsplit(raw)
    except ValueError:
        return None
    # The target is an archive member name. Keep literal percent escapes.
    path = target.path
    if (link.get("TargetMode", "Internal") != "Internal" or target.scheme or target.netloc
            or not path or any(mark in raw for mark in ("?", "#", "\\"))
            or any(ord(c) < 32 for c in raw)):
        return None
    name = posixpath.normpath(path[1:] if path.startswith("/") else posixpath.join("word", path))
    key = _part_key(name)
    if key in (".", "..", _part_key(MANIFEST), DOCUMENT) or name.startswith(("../", "/")) or key.endswith(".rels"):
        return None
    return name


def _document_links(root):
    return list(root.iterchildren(_REL + "Relationship")) if root is not None and root.tag == _REL + "Relationships" else None


def _linked_comment_part(names, links, relationship_part, legacy, types):
    if relationship_part is None:
        return _part_name(names, legacy) if _part_key(legacy) in names else (None, None)
    if links is None:
        return relationship_part, "Document relationships could not be read."
    matches = [link for link in links if link.get("Type") in types]
    if not matches:
        return None, None
    if len(matches) != 1:
        return relationship_part, "More than one relationship names this comment source."
    name = _relationship_target(matches[0])
    if name is None:
        return relationship_part, "The comment relationship does not name an internal DOCX part."
    return _part_name(names, name)


def _legacy_story_role(name):
    if name in ("word/footnotes.xml", "word/endnotes.xml"):
        return name.removeprefix("word/").removesuffix(".xml")
    if name.endswith(".xml"):
        for role in ("header", "footer"):
            if name.startswith("word/" + role):
                return role
    return None


def _linked_stories(parts, names, links, relationship_part, document):
    legacy = {}
    if relationship_part is None:
        # Legacy inputs may identify a story by its filename and Word root.
        legacy = {name: _STORY_ROLES[role] for name in parts
                  if (role := _legacy_story_role(_part_key(name))) and names[_part_key(name)] == name}

    linked, issues, uncertain_roles = {}, {}, set()
    type_roles = {prefix + role: role for role in _STORY_ROLES for prefix in _OFFICE_REL}
    if links is None and relationship_part is not None:
        issues[relationship_part] = "Document relationships could not be read."
        uncertain_roles.update(_STORY_ROLES)
        return linked, legacy, issues, uncertain_roles

    by_id = {}
    for link in links or ():
        by_id.setdefault(link.get("Id"), []).append(link)
    for link in links or ():
        if (role := type_roles.get(link.get("Type"))) is None:
            continue
        rid = link.get("Id")
        if not rid or len(by_id[rid]) != 1:
            issues[relationship_part] = f"A {role} relationship has a missing or repeated ID ({rid or 'missing'})."
            uncertain_roles.add(role)
            continue
        target = _relationship_target(link)
        if target is None:
            issues[relationship_part] = "A story relationship does not name an internal DOCX part."
            uncertain_roles.add(role)
            continue
        name, issue = _part_name(names, target)
        if issue:
            issues[name] = issue
            uncertain_roles.add(role)
        else:
            linked.setdefault(name, set()).add(_STORY_ROLES[role])

    dangling = []
    for ref in document.iter(W + "headerReference", W + "footerReference"):
        if next(ref.iterancestors(W + "sectPrChange"), None) is not None:
            continue
        role = "header" if ref.tag == W + "headerReference" else "footer"
        rid = next((value for attr in _RELATIONSHIP_IDS if (value := ref.get(attr))), None)
        matches = by_id.get(rid, ()) if rid else ()
        if len(matches) != 1 or type_roles.get(matches[0].get("Type")) != role:
            dangling.append(f"{role} {rid or '(no r:id)'}")
            uncertain_roles.add(role)
    if dangling:
        part = relationship_part or DOCUMENT_RELS
        prefix = issues[part] + " " if part in issues else ""
        issues[part] = prefix + "Section relationships could not be resolved: " + ", ".join(sorted(set(dangling))) + "."
    return linked, legacy, issues, uncertain_roles


def _package(source: BinaryIO | Path):
    parts, unread = {}, {}
    with ZipFile(source) as archive:
        infos = archive.infolist()
        if len(infos) > 5000 or sum(i.file_size for i in infos) > 64 * 1024 * 1024:
            raise CheckLimit("This checker supports up to 64 MiB expanded and 5,000 ZIP members. "
                             "Editing is still available; preservation was not checked.")
        if len({i.filename for i in infos}) != len(infos):
            raise ValueError("Duplicate ZIP members.")
        names = _part_names(entry.filename for entry in infos if not entry.is_dir())
        document, doc_issue = _part_name(names, DOCUMENT)
        manifest, manifest_issue = _part_name(names, MANIFEST)
        if doc_issue or manifest_issue:
            raise ValueError(f"Not a DOCX package. {document if doc_issue else manifest}: {doc_issue or manifest_issue}")
        essential = {document, manifest}
        for entry in infos:
            if entry.is_dir():
                continue
            name = entry.filename
            try:
                parts[name] = archive.read(entry)
            except (BadZipFile, EOFError, RuntimeError, OSError, zlib.error, lzma.LZMAError) as exc:
                if name in essential:
                    raise ValueError(f"{name}: {exc}") from exc
                parts[name] = None
                unread[name] = {"stage": "zip_reading", "issue": str(exc)}

    roots = {}
    for name in essential:
        try:
            roots[name] = _xml_root(parts[name])
        except (ET.XMLSyntaxError, ValueError) as exc:
            raise ValueError(f"{name}: {exc}") from exc
    if roots[document].tag != W + "document":
        raise ValueError(f"{document}: Unexpected document root.")
    if roots[manifest].tag != "{http://schemas.openxmlformats.org/package/2006/content-types}Types":
        raise ValueError(f"{manifest}: Unexpected package manifest root.")

    relationship_part = None
    if _part_key(DOCUMENT_RELS) in names:
        relationship_part, issue = _part_name(names, DOCUMENT_RELS)
        if not issue and parts[relationship_part] is not None:
            try:
                roots[relationship_part] = _xml_root(parts[relationship_part])
            except (ET.XMLSyntaxError, ValueError) as exc:
                unread[relationship_part] = {"stage": "parsing", "issue": str(exc)}
        elif issue:
            unread[relationship_part] = {"stage": "resolution", "issue": issue}

    links = _document_links(roots.get(relationship_part))
    sources = {role: _linked_comment_part(names, links, relationship_part, legacy, types)
               for role, (legacy, _, types) in _COMMENT_SOURCES.items()}
    linked, legacy, story_issues, uncertain_roles = _linked_stories(parts, names, links, relationship_part, roots[document])
    xml_targets = linked.keys() | {part for part, issue in sources.values() if part and not issue}
    for name, data in parts.items():
        if data is None or name in essential or _part_key(name) == _part_key(DOCUMENT_RELS):
            continue
        if name not in xml_targets and not _part_key(name).endswith((".xml", ".rels")):
            continue
        try:
            roots[name] = _xml_root(data)
        except (ET.XMLSyntaxError, ValueError) as exc:
            unread[name] = {"stage": "parsing", "issue": str(exc)}

    for role, (part, issue) in sources.items():
        if part and not issue:
            root = roots.get(part)
            if root is None or root.tag != _COMMENT_SOURCES[role][1]:
                failure = unread[part]["issue"] if part in unread else "Unexpected root element."
                sources[role] = part, f"Could not read the selected comment source: {failure}"

    stories = {document: roots[document]}
    comment_parts = {part for part, issue in sources.values() if part and not issue}
    for name, expected in linked.items():
        if name in roots and roots[name].tag in expected:
            stories[name] = roots[name]
        else:
            story_issues[name] = unread[name]["issue"] if name in unread else "Unexpected root in a linked story."
            if name not in unread:
                uncertain_roles.update(_STORY_ROLE_FOR_TAG[tag] for tag in expected)
    for name, expected in legacy.items():
        if name in linked or name in comment_parts:
            continue
        if name in roots and roots[name].tag == expected:
            stories[name] = roots[name]
        elif name in unread and name not in {part for part, _ in sources.values()}:
            story_issues[name] = unread[name]["issue"]
    if sources["comments"] == (None, None) and any(
            next(root.iter(*COMMENT_ANCHORS), None) is not None for root in stories.values()):
        sources["comments"] = relationship_part or DOCUMENT_RELS, "Comment anchors exist but no comments part could be located."
    return parts, roots, unread, sources, (stories, story_issues, uncertain_roles)


def _package_with_hash(path: Path):
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        parts, roots, unread, sources, stories = _package(source)
        source.seek(0)
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise OSError(f"DOCX changed during reading: {path}")
    return parts, roots, unread, sources, stories, digest


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


def _story_without_comment_anchors(root):
    if next(root.iter(*COMMENT_ANCHORS), None) is None:
        return root
    root = deepcopy(root)
    for anchor in list(root.iter(*COMMENT_ANCHORS)):
        parent = anchor.getparent()
        if anchor.tag == W + "commentReference" and parent.tag == W + "r" and all(
                c is anchor or c.tag == W + "rPr" for c in parent) and not any(
                    e.tag in REVISION_TAGS for e in parent.iter()):
            parent.getparent().remove(parent)
        else:
            parent.remove(anchor)
    return root


def _undo(root, new):
    """Copy of a story part with new revisions undone."""
    root = deepcopy(root)
    problems = []

    def fresh(tags):
        return [e for e in root.iter(*tags) if _key(e) in new]

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


def _parse_date(value, *, allow_local=False):
    if not value:
        return None, "Missing date."
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, "Invalid date/time."
    if allow_local and stamp.tzinfo is None and "T" in value:
        return stamp, None
    # fromisoformat normalizes offsets such as +03:75 and accepts offsets beyond XML's range.
    if "T" not in value or not re.search(r"(?:Z|[+-](?:(?:0[0-9]|1[0-3]):[0-5][0-9]|14:00))\Z", value):
        return None, "Use a date and time with a valid explicit timezone: Z or +/-HH:MM, up to +/-14:00."
    return stamp, None


def _future_date_issue(stamp, checked_at):
    if stamp > checked_at + timedelta(minutes=5):
        return "More than 5 minutes in the future at this check; verify the clock and timezone."
    return None


def _date_issue(value, checked_at):
    stamp, issue = _parse_date(value)
    return issue or _future_date_issue(stamp, checked_at)


def _revision_date_issue(date, utc, checked_at):
    if utc is None:
        return _date_issue(date, checked_at)
    instant, issue = _parse_date(utc)
    if issue:
        return f"Invalid dateUtc. {issue}"
    if instant.utcoffset() != timedelta(0):
        return "dateUtc must use UTC."
    future_issue = _future_date_issue(instant, checked_at)
    display, display_issue = _parse_date(date, allow_local=True)
    if display is not None:
        # Compare clock digits loosely, since Word can write local digits with a Z suffix.
        if abs(display.replace(tzinfo=None) - instant.replace(tzinfo=None)) > timedelta(days=1):
            display_issue = "w:date and dateUtc differ by more than a day."
        elif display.tzinfo is not None and not date.endswith("Z") and display != instant:
            display_issue = "w:date and dateUtc refer to different instants."
    return " ".join(issue for issue in (future_issue, display_issue) if issue) or None


def _story(part, a, b, author, checked_at):
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
        trial, _ = _undo(b, new)
        if _sig(a) != _sig(trial):
            for extra in (same_stamp, _added_insertions(a, trial, same_stamp)):
                if not extra:
                    continue
                alternative, problems = _undo(b, new | extra)
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
        utc = e.get(REVISION_DATE_UTC)
        if issue := _revision_date_issue(date, utc, checked_at):
            finding = {**where, "date": date, "issue": issue}
            if utc is not None:
                finding["dateUtc"] = utc
            found["dates"].append(finding)
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
    restored, problems = _undo(b, new)
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


def _package_issues(before, after, left, right, unread, comment_parts, stories, part_pairs):
    issues, metadata, unchecked = [], [], []
    old_comments, new_comments = comment_parts
    old_stories, new_stories = stories
    removed = sorted(old for old, new in part_pairs if new is None)
    if removed:
        issues.append({"issue": "parts removed", "parts": removed})
    added = sorted(new for old, new in part_pairs
                   if old is None and new not in new_comments and not _part_key(new).endswith(".rels"))
    if added:
        issues.append({"issue": "unexpected new parts", "parts": added})
    for old_name, name in part_pairs:
        if old_name is None or name is None or before[old_name] == after[name]:
            continue
        if (old_name in old_stories and name in new_stories) or (old_name in old_comments and name in new_comments):
            continue
        a, b = left.get(old_name), right.get(name)
        if before[old_name] is None or after[name] is None:
            continue
        key = _part_key(name)
        if old_name in unread or name in unread:
            issues.append({"issue": "bytes changed; auxiliary XML comparison incomplete", "part": name})
        elif key.startswith("docprops/"):
            metadata.append(name)
        elif a is None or b is None:
            issues.append({"issue": "binary part changed", "part": name})
        elif key == _part_key(MANIFEST) or key.endswith(".rels"):
            if _package_entries(a) - _package_entries(b):
                issues.append({"issue": "existing package entries removed or changed", "part": name})
        elif key == "word/styles.xml":
            old, new = _entries(a, W + "style", W + "styleId"), _entries(b, W + "style", W + "styleId")
            changed = sorted(k for k, e in old.items() if k not in new or _sig(e) != _sig(new[k]))
            repeated = sorted(_repeated(b, W + "style", W + "styleId") - _repeated(a, W + "style", W + "styleId"))
            if changed or repeated or _other_children(a, {W + "style"}) != _other_children(b, {W + "style"}):
                issues.append({"issue": "existing styles changed (new styles are allowed)", "part": name,
                               "styles": changed[:20], "repeated_ids": repeated[:20]})
        elif key == "word/settings.xml":
            skipped = {W + "rsids", W + "trackRevisions"}
            if _other_children(a, skipped) != _other_children(b, skipped):
                issues.append({"issue": "settings changed beyond revision ids and the track-changes switch",
                               "part": name})
        elif key.startswith("word/") and _sig(a) != _sig(b):
            issues.append({"issue": "content changed", "part": name})
        elif not key.startswith("word/"):
            # Custom data outside word/: every character counts, including whitespace.
            canonical = []
            for side, source, root in (("original", old_name, a), ("candidate", name, b)):
                try:
                    canonical.append(ET.tostring(root, method="c14n"))
                except ET.C14NError as exc:
                    unchecked.append({"part": source, "side": side, "stage": "canonicalization", "issue": str(exc)})
            if len(canonical) != 2:
                issues.append({"issue": "bytes changed; auxiliary XML comparison incomplete", "part": name})
            elif canonical[0] != canonical[1]:
                issues.append({"issue": "content changed (custom data, compared exactly)", "part": name})
    return issues, metadata, unchecked


def _comment_report(inventory):
    from veqtor_workspace.reading import _clip_strikes, _mark_strikes

    items = []
    for note in inventory["items"][:5]:
        text = note["text"][:240]
        spans = _clip_strikes(note.get("strikethrough_spans", ()), 0, len(text))
        items.append({**{key: value for key, value in note.items() if key not in ("anchor_markup", "strikethrough_spans")},
                      "text": text, "paragraphs": note["paragraphs"][:5],
                      **({"strikethrough_spans": spans, "formatted_excerpt": _mark_strikes(text, spans)} if spans else {}),
                      **({"text_truncated": True} if len(note["text"]) > 240 else {}),
                      **({"paragraphs_truncated": True} if len(note["paragraphs"]) > 5 else {}),
                      **({"anchor_markup_truncated": True} if len(note["anchor_markup"]) > 240 else {})})
    return {"total": inventory["total"], "items": items,
            "remaining_items": len(inventory["items"]) - len(items), "issues": inventory["issues"]}


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
        before, left, unread_left, original_sources, (old_stories, unread_old_story, uncertain_old_roles), original_hash = _package_with_hash(original)
        after, right, unread_right, candidate_sources, (new_stories, unread_new_story, uncertain_new_roles), candidate_hash = _package_with_hash(candidate)
    except CheckLimit as exc:
        return {**evidence, "status": "not_checked", "summary": {"errors": [], "observations": [], "warnings": [],
                "not_checked": ["docx_preservation"]},
                "checks": [{"check": "docx_preservation", "status": "not_checked", "detail": str(exc)}]}
    except (OSError, ValueError, ET.XMLSyntaxError, BadZipFile, RuntimeError) as exc:
        return {**evidence, "status": "error", "summary": {"errors": ["package_and_xml"], "observations": [], "warnings": [], "not_checked": []},
                "checks": [{"check": "package_and_xml", "status": "error", "detail": str(exc)}]}
    from veqtor_workspace.reading import change_summary, comment_inventory, preview_guidance

    story_unread = [{"part": part, "side": side, "issue": issue}
                    for side, failures in (("original", unread_old_story), ("candidate", unread_new_story))
                    for part, issue in sorted(failures.items())]
    excluded = {_part_key(part) for part in unread_old_story.keys() | unread_new_story.keys()}
    excluded_stories = set(unread_old_story) | set(unread_new_story)
    for stories, opposite, roles in ((old_stories, new_stories, uncertain_new_roles),
                                     (new_stories, old_stories, uncertain_old_roles)):
        opposite_names = _part_names(opposite)
        for part, root in stories.items():
            key = _part_key(part)
            if key in excluded or (not opposite_names.get(key) and _STORY_ROLE_FOR_TAG.get(root.tag) in roles):
                excluded_stories.add(part)
    unread = [{"part": part, "side": side, **details}
              for side, failures, story_failures in (("original", unread_left, unread_old_story),
                                                    ("candidate", unread_right, unread_new_story))
              for part, details in sorted(failures.items()) if part not in story_failures]
    add("package_and_xml", not (unread or story_unread),
        {"unread_auxiliary_xml": len(unread), "unread_story_parts": len(story_unread)}
        if unread or story_unread else None, "warning")
    part_pairs = list(_part_pairs(before, after))
    changed = sorted(new or old for old, new in part_pairs if old is None or new is None
                     or (before[old] is not None and after[new] is not None and before[old] != after[new]))
    original_facts = _comment_report(comment_inventory(left, original_sources, old_stories))
    candidate_facts = _comment_report(comment_inventory(right, candidate_sources, new_stories))
    counts = original_facts["total"], candidate_facts["total"]
    comment_facts = {
        "original": original_facts, "candidate": candidate_facts,
        "net_change": counts[1] - counts[0] if None not in counts else None,
        "scope": "Independent file snapshots. Comment IDs are local to each file.",
        "read_more": "Use read_docx(path, format='data') on each file for all comment records and full text.",
    }

    totals = {name: [] for name in FINDINGS}
    new_count = split = inherited = 0
    story_before = {part: root for part, root in old_stories.items() if part not in excluded_stories}
    story_after = {part: root for part, root in new_stories.items() if part not in excluded_stories}
    for old_part, new_part in _part_pairs(story_before, story_after):
        part = new_part or old_part
        a, b = story_before.get(old_part), story_after.get(new_part)
        if a is None or b is None:
            totals["untracked"].append({"part": part, "differences": [{"where": "whole part", "change": "added or removed"}]})
            continue
        found = _story(part, _story_without_comment_anchors(a), _story_without_comment_anchors(b),
                       author, checked_at)
        for name in FINDINGS:
            totals[name].extend(found[name])
        new_count, split, inherited = new_count + found["new"], split + found["split"], inherited + found["inherited"]

    comment_parts = tuple({part for part, issue in sources.values() if part and not issue}
                          for sources in (original_sources, candidate_sources))
    package_issues, metadata, uncanonical = _package_issues(
        before, after, left, right, unread_left.keys() | unread_right.keys(), comment_parts,
        (old_stories, new_stories), part_pairs)
    counterparts = {"original": dict(part_pairs), "candidate": {new: old for old, new in part_pairs}}
    unchecked = []
    for item in unread + uncanonical:
        part, side = item["part"], item["side"]
        other = counterparts[side].get(part)
        current_parts, other_parts = (before, after) if side == "original" else (after, before)
        current, opposite = current_parts.get(part), other_parts.get(other, b"")
        unchecked.append({**item, "bytes_changed": None if current is None or opposite is None else other is None or current != opposite})
    add("package_parts", not (package_issues or unchecked), {"issues": package_issues, "metadata_changed": metadata},
        "observation" if package_issues else "not_checked")
    if unchecked:
        add("auxiliary_xml_comparison", False, unchecked, "not_checked")
    if story_unread:
        add("story_coverage", False, story_unread, "not_checked")
    if original_facts["issues"] or candidate_facts["issues"]:
        add("comment_facts", False, {"note": "See comment_facts for per-file coverage."}, "not_checked")

    def add_revision_check(name, findings, detail, problem="observation"):
        if story_unread:
            if isinstance(detail, list):
                detail = {"issues": detail}
            detail = {**(detail or {}), "unread_story_parts": sorted(excluded_stories)}
            if not findings:
                detail.pop("note", None)
        add(name, not (findings or story_unread), detail, problem if findings else "not_checked")

    add_revision_check("new_revision_authors", totals["authors"],
        {"new_revisions_in_readable_stories" if story_unread else "new_revisions": new_count,
         "unexpected_authors": totals["authors"][:20]})
    add_revision_check("new_revision_dates", totals["dates"],
        {"issues": totals["dates"][:20], "count": len(totals["dates"])} if totals["dates"] else None, "warning")
    add_revision_check("new_revision_ids", totals["ids"], totals["ids"][:20])
    add_revision_check("new_revision_structure", totals["structure"], {
        "issues": totals["structure"][:20],
        "note": "Word 16.95 opens a file with these structures only as a repaired copy."})
    add_revision_check("new_revision_placement", totals["placement"], {
        "placements": totals["placement"][:20],
        "note": "Compatibility warning, not damage. Word writes a new insertion beside other revisions, not "
                "inside them. Word 16.95 opens the tested forms (an insertion inside another author's insertion, "
                "a deletion inside a deletion) and accepts or rejects them as expected. Other editors and "
                "checkers were not tested."}, "warning")
    add_revision_check("redline_quality", totals["quality"], {
        "findings": totals["quality"][:20], "count": len(totals["quality"]),
        "scope": "Advisory comparison of equal word edges in adjacent new plain-text revisions, "
                 "with identical run properties, up to 20,000 characters per side. "
                 "Not a complete minimality check; nested history, moves, fields and complex formatting are skipped. "
                 "No document changes or save blocking."}, "warning")
    add("existing_revisions_preserved", not (totals["existing"] or story_unread), {
        "changed_or_removed": totals["existing"][:20], "count": len(totals["existing"]),
        "split_pieces_of_existing_revisions": split,
        "scope": "All revision kinds after undoing new changes; splitting any author's earlier revision "
                 "is allowed when its text, author, date and context are kept. Origins are inferred from the snapshot; "
                 "a mismatch does not by itself prove damage when old/new metadata is ambiguous.",
        **({"unread_story_parts": sorted(excluded_stories)} if story_unread else {})},
        "observation" if totals["existing"] else "not_checked")
    add("untracked_changes", not (totals["untracked"] or story_unread), {
        "parts": totals["untracked"], "not_undone": totals["not_undone"],
        "comment_anchors": "Excluded from preservation diagnostics. Raw markup differences remain in changes.",
        "scope": "Undo new revisions and compare readable story XML. Formatting of newly inserted paragraphs is not compared.",
        **({"unread_story_parts": sorted(excluded_stories)} if story_unread else {})},
        "observation" if totals["untracked"] else "not_checked")
    if inherited:
        checks.append({"check": "inherited_deletion_text", "status": "not_checked",
                       "detail": {"ordinary_text_nodes_inside_existing_deletions": inherited,
                                  "note": "Pre-existing deletion structure needs review; no automatic repair performed."}})
    if totals["origins"]:
        checks.append({"check": "revision_origins", "status": "not_checked", "detail": totals["origins"]})
    checks.append({"check": "layout_and_legal_intent", "status": "not_checked",
                   "detail": "Native Word opening/accept-reject, XSD/relationship validation, layout, fields and legal meaning are not checked."})
    group = {s: [c["check"] for c in checks if c["status"] == s] for s in ("observation", "warning", "not_checked")}
    changes = change_summary(story_before, story_after, changes_start)
    if story_unread:
        changes["scope"] += " Unavailable story parts are excluded from this list."
    return {**evidence, "status": "diagnostic",
            "summary": {"errors": [], "observations": group["observation"], "warnings": group["warning"], "not_checked": group["not_checked"]},
            "checks": checks, "changed_parts": changed, "comment_facts": comment_facts,
            **({"unchecked_auxiliary_xml": unchecked} if unchecked else {}),
            **({"unchecked_story_xml": story_unread} if story_unread else {}),
            "original_sha256": original_hash,
            "candidate_sha256": candidate_hash,
            "changes": changes,
            "preview_guidance": preview_guidance(story_after, story_before)}
