# SPDX-License-Identifier: Apache-2.0
"""Build the native ChatGPT installer. Public distribution requires Developer ID
signing and successful Apple notarization; the default build is a local candidate.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import plistlib
import shutil
import struct
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UV_VERSION = "0.11.28"
UV_ARCHIVE = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-aarch64-apple-darwin.tar.gz"
UV_SHA256 = "33540eb7c883ab857eff79bd5ac2aa31fe27b595abecb4a9c003a2c998447232"


def build(out: Path) -> Path:
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("build_mcpb", ROOT / "packaging/build_mcpb.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = module.build(out)
    manifest = json.loads((ROOT / "packaging/manifest.json").read_text())
    version = manifest["version"]
    app = out / "Veqtor Workspace Setup.app"
    if app.exists():
        raise FileExistsError(f"Use a fresh output directory; refusing to overwrite {app}")
    resources = app / "Contents/Resources"
    executable = app / "Contents/MacOS/Veqtor Workspace Setup"
    resources.mkdir(parents=True)
    executable.parent.mkdir()
    helpers = app / "Contents/Helpers"
    helpers.mkdir()
    shutil.copy2(payload, resources / "workspace.mcpb")
    for name in ("configure.py", "probe.py", "veqtor-launch.py"):
        shutil.copy2(ROOT / "packaging/chatgpt" / name, resources / name)
    shutil.copy2(ROOT / "packaging/icon.png", resources / "icon.png")
    shutil.copy2(ROOT / "LICENSE", resources / "LICENSE")
    icon = (resources / "icon.png").read_bytes()
    (resources / "icon.icns").write_bytes(b"icns" + struct.pack(">I", len(icon) + 16) + b"ic09" + struct.pack(">I", len(icon) + 8) + icon)
    cache = ROOT / "dist/downloads"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"uv-{UV_VERSION}-macos-arm64.tar.gz"
    if not archive.exists():
        temporary = archive.with_suffix(".partial")
        try:
            subprocess.run(["curl", "--fail", "--location", "--silent", "--show-error", "--continue-at", "-",
                            "--retry", "2", "--max-time", "600",
                            "--output", str(temporary), UV_ARCHIVE], check=True, timeout=1820)
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    data = archive.read_bytes()
    if hashlib.sha256(data).hexdigest() != UV_SHA256:
        raise ValueError("The uv download does not match the pinned SHA-256")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        member = tar.getmember("uv-aarch64-apple-darwin/uv")
        if not member.isfile():
            raise ValueError("Unexpected uv archive entry")
        (helpers / "uv").write_bytes(tar.extractfile(member).read())
    (helpers / "uv").chmod(0o755)
    (resources / "THIRD_PARTY_NOTICES.txt").write_text(
        f"uv {UV_VERSION} is Copyright Astral Software Inc. and contributors.\n"
        "Licensed under either Apache-2.0 or MIT.\n"
        f"Source and license: https://github.com/astral-sh/uv/tree/{UV_VERSION}\n"
        "Python and project dependencies are downloaded using the bundled frozen uv.lock.\n\n"
        + (ROOT / "packaging/chatgpt/UV-LICENSE-MIT.txt").read_text()
    )
    (resources / "release.json").write_text(json.dumps({"version": version,
        "payload_sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
        "uv_version": UV_VERSION, "uv_archive_sha256": UV_SHA256}, indent=2) + "\n")
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleName": "Veqtor Workspace Setup", "CFBundleDisplayName": "Veqtor Workspace Setup",
        "CFBundleIdentifier": "pro.veqtor.workspace.setup", "CFBundleVersion": version,
        "CFBundleShortVersionString": version, "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "Veqtor Workspace Setup", "CFBundleIconFile": "icon.icns",
        "LSMinimumSystemVersion": "13.0", "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication", "NSHumanReadableCopyright": "Copyright Ilya Shilov. Apache-2.0."
    }))
    common = ["xcrun", "swiftc", "-swift-version", "5", "-O", "-target", "arm64-apple-macosx13.0", "-parse-as-library"]
    subprocess.run(common + [str(ROOT / "packaging/chatgpt/InstallerEngine.swift"),
        str(ROOT / "packaging/chatgpt/Setup.swift"), "-o", str(executable)], check=True)
    subprocess.run(common + [str(ROOT / "packaging/chatgpt/InstallerEngine.swift"),
        str(ROOT / "packaging/chatgpt/VerifyMain.swift"), "-o", str(out / "verify-installer")], check=True)
    # Local candidates are deliberately not labelled as signed public releases.
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
    return app


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    print(build(p.parse_args().out))
