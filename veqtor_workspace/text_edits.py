# SPDX-License-Identifier: Apache-2.0
"""Small text-only edit planner. The model still handles the document's actual XML."""

import difflib
import re

TOKEN = re.compile(r"\w+|[^\w\s]|\s+", re.UNICODE)


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
