# SPDX-License-Identifier: Apache-2.0
"""Optional helpers for model-authored Word edits."""

import difflib
import os
import re
from datetime import datetime, timezone

TOKEN = re.compile(r"\w+|[^\w\s]|\s+", re.UNICODE)
_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_IGNORABLE = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable"
_UTC_NS = "http://schemas.microsoft.com/office/word/2023/wordml/word16du"
REVISION_DATE_UTC = f"{{{_UTC_NS}}}dateUtc"


def stamp_revision_time(revision):
    """Set the local display time and precise UTC instant on a new, attached lxml revision."""
    if revision.getparent() is None:
        raise ValueError("Insert the revision into its document before stamping its time.")
    host_time = os.environ.get("VEQTOR_LOCAL_NOW")
    local = datetime.fromisoformat(host_time) if host_time else datetime.now().astimezone()
    revision.set(_WORD + "date", local.replace(tzinfo=None).isoformat(timespec="seconds"))
    utc = local.astimezone(timezone.utc).isoformat(timespec="seconds")
    revision.set(REVISION_DATE_UTC, utc.replace("+00:00", "Z"))
    # Declare the extension on the new revision so existing markup stays intact.
    prefix = next(p for p, uri in revision.nsmap.items() if p and uri == _UTC_NS)
    ignorable = revision.get(_IGNORABLE, "").split()
    if prefix not in ignorable:
        revision.set(_IGNORABLE, " ".join([*ignorable, prefix]))


def minimal_edits(before: str, after: str) -> list[dict]:
    """Return changed spans in ORIGINAL Python character offsets, keeping equal text.

    Words/numbers stay whole; punctuation and whitespace are separate tokens. Find
    the full `before` passage once, then apply these spans from right to left (or
    construct the result in one pass). start == end is an insertion. This does not
    edit XML, choose legal wording, accept history or normalize any whitespace.
    """
    left, right = list(TOKEN.finditer(before)), list(TOKEN.finditer(after))
    a, b = [m.group() for m in left], [m.group() for m in right]
    offsets = [m.start() for m in left] + [len(before)]
    # Repeated spaces must not outweigh a matching word when aligning passages.
    matcher = difflib.SequenceMatcher(lambda token: token.isspace(), a, b, autojunk=False)
    return [{"start": offsets[i], "end": offsets[j], "old": before[offsets[i]:offsets[j]],
             "new": "".join(b[k:l])}
            for kind, i, j, k, l in matcher.get_opcodes()
            if kind != "equal"]
