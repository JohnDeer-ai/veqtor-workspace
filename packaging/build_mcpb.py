# SPDX-License-Identifier: Apache-2.0
"""Build the pilot as a Claude Desktop extension (.mcpb), installed and managed like Veqtor.

Usage: python packaging/build_mcpb.py --out DIR [--default-author NAME]
Defaults only prefill the install dialog; the package itself stays machine-independent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATE = (2020, 2, 2, 0, 0, 0)


def payloads(author: str | None = None) -> dict[str, bytes]:
    manifest = json.loads((ROOT / "packaging/manifest.json").read_text(encoding="utf-8"))
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if manifest["version"] != version:
        raise ValueError(f"manifest version {manifest['version']} differs from pyproject version {version}")
    if author:
        manifest["user_config"]["author"]["default"] = author
    files = {"manifest.json": (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()}
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE"):
        files[name] = (ROOT / name).read_bytes()
    for path in sorted((ROOT / "veqtor_workspace").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            files[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    return files


def build(out: Path, author: str | None = None) -> Path:
    files = payloads(author)
    manifest = json.loads(files["manifest.json"])
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{manifest['name']}-{manifest['version']}.mcpb"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=DATE)
            info.external_attr = 0o644 << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--default-author")
    options = parser.parse_args()
    target = build(options.out, options.default_author)
    print(hashlib.sha256(target.read_bytes()).hexdigest(), target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
