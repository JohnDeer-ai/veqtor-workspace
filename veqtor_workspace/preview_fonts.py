# SPDX-License-Identifier: Apache-2.0
"""Small, advisory font observations from the actual preview PDF, not a style engine."""

import re
import unicodedata
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

from lxml import etree as ET
from pypdf import PdfReader

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
LIMIT = 12


def _text(value):
    return "".join(unicodedata.normalize("NFKC", value).split())


def _font(value):
    return re.sub(r"[^\w]", "", value).casefold()


def _matches(requested, rendered):
    # PostScript names often append style/MT/PS suffixes. These are not evidence
    # of substitution; other aliases are reported as possibilities, not errors.
    return _font(rendered).startswith(_font(requested))


def font_report(source: Path, directory: Path) -> dict:
    try:
        pdfs = list(directory.glob("*.pdf"))
        if len(pdfs) != 1:
            return {"status": "not_available", "detail": "No single preview PDF."}
        fonts, spans = set(), []
        for number, page in enumerate(PdfReader(pdfs[0]).pages, 1):
            def visit(text, _cm, _tm, font, _size, page_number=number):
                name = (font or {}).get("/BaseFont")
                if not text.strip() or not name:
                    return
                name = re.sub(r"^[A-Z]{6}\+", "", str(name).lstrip("/"))
                fonts.add(name)
                if spans and spans[-1][:2] == [page_number, name]:
                    spans[-1][2] += _text(text)
                else:
                    spans.append([page_number, name, _text(text)])
            page.extract_text(visitor_text=visit)

        declared, runs = set(), []
        with ZipFile(source) as archive:
            for name in archive.namelist():
                if not name.startswith("word/") or not name.endswith(".xml"):
                    continue
                root = ET.fromstring(archive.read(name), ET.XMLParser(resolve_entities=False, no_network=True))
                for spec in root.iter(W + "rFonts"):
                    declared.update(spec.get(W + key) for key in ("ascii", "hAnsi", "cs", "eastAsia") if spec.get(W + key))
                if name == "word/document.xml":
                    for run in root.iter(W + "r"):
                        if any(p.tag in (W + "del", W + "moveFrom") for p in run.iterancestors()):
                            continue
                        spec = run.find(W + "rPr/" + W + "rFonts")
                        text = _text("".join(run.itertext(W + "t")))
                        if spec is None or len(text) < 20:
                            continue
                        requested = spec.get(W + "ascii")
                        # Only an explicit, uniform Latin/Cyrillic assignment is
                        # comparable here; inherited/theme/complex-script fonts aren't resolved.
                        if (requested and spec.get(W + "hAnsi") == requested
                                and not any(k.endswith("Theme") for k in spec.attrib)
                                and all(not c.isalpha() or "LATIN" in unicodedata.name(c, "")
                                        or "CYRILLIC" in unicodedata.name(c, "") for c in text)):
                            runs.append((requested, text))
        counts = Counter(text for _, text in runs)
        differences = {}
        for requested, text in runs:
            if counts[text] != 1:
                continue
            matches = [(page, font) for page, font, output in spans if output.count(text) == 1]
            if len(matches) == 1 and not _matches(requested, matches[0][1]):
                page, actual = matches[0]
                differences.setdefault((requested, actual), set()).add(page)
        absent = sorted(f for f in declared if not any(_matches(f, actual) for actual in fonts))
        substitutions = [{"requested": requested, "rendered": actual, "example_pages": sorted(pages)[:3],
                          "matched_page_count": len(pages)}
                         for (requested, actual), pages in sorted(differences.items())]
        return {"status": "observed", "pdf_fonts": sorted(fonts)[:LIMIT], "pdf_font_count": len(fonts),
                "declared_but_not_seen": absent[:LIMIT], "declared_but_not_seen_count": len(absent),
                "possible_substitutions": substitutions[:LIMIT], "possible_substitution_count": len(substitutions),
                "scope": "PDF fonts are observed in extracted text. Possible substitutions match unique directly "
                         "formatted text; aliases may differ. Declared fonts may belong to unused styles/history. "
                         "Theme/style inheritance, glyph fallback and installed-font availability are not verified. "
                         "An empty list is not proof of font fidelity; do not change layout solely on these lists."}
    except Exception as exc:  # noqa: BLE001 - advisory inspection must not discard usable preview images
        return {"status": "not_available", "detail": f"Font inspection failed: {type(exc).__name__}"}
