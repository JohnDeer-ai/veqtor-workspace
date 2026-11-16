# SPDX-License-Identifier: Apache-2.0
"""Sign, notarize and verify the ChatGPT installer for public distribution.

Requires an existing Developer ID Application identity and an existing notarytool
keychain profile. Never accepts passwords or exports signing keys.
"""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from package_chatgpt_app import package


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def release(app: Path, out: Path, identity: str, profile: str):
    if not identity.startswith("Developer ID Application:"):
        raise ValueError("Public distribution requires Developer ID Application, not Apple Development or ad-hoc signing")
    metadata = json.loads((app / "Contents/Resources/release.json").read_text())
    version = metadata["version"]
    if not app.is_dir() or app.suffix != ".app":
        raise ValueError("Pass the built Veqtor Workspace Setup.app")
    out.mkdir(parents=True, exist_ok=True)
    dmg = out / f"veqtor-workspace-{version}-chatgpt-macos-arm64.dmg"
    if dmg.exists():
        raise FileExistsError(dmg)
    # Sign nested executable code first; do not use --deep as a signing shortcut.
    run("codesign", "--force", "--sign", identity, "--options", "runtime", "--timestamp", app / "Contents/Helpers/uv")
    run("codesign", "--force", "--sign", identity, "--options", "runtime", "--timestamp", app)
    run("codesign", "--verify", "--deep", "--strict", "--verbose=2", app)
    dmg = package(app, out, unsigned=False)
    run("codesign", "--force", "--sign", identity, "--timestamp", dmg)
    response = subprocess.run(["xcrun", "notarytool", "submit", str(dmg), "--keychain-profile", profile,
                               "--wait", "--output-format", "json"], check=True, capture_output=True, text=True)
    result = json.loads(response.stdout)
    (out / "notarization.json").write_text(json.dumps(result, indent=2) + "\n")
    if result.get("status") != "Accepted":
        raise RuntimeError("Apple did not accept this build. Do not publish the disk image; inspect notarization.json.")
    run("xcrun", "stapler", "staple", dmg)
    run("xcrun", "stapler", "validate", dmg)
    run("spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "--verbose=4", dmg)
    run("spctl", "--assess", "--type", "execute", "--verbose=4", app)
    checksum = hashlib.sha256(dmg.read_bytes()).hexdigest()
    (out / (dmg.name + ".sha256")).write_text(checksum + "  " + dmg.name + "\n")
    print(json.dumps({"artifact": str(dmg), "sha256": checksum, "notarization": "Accepted"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--notary-profile", required=True)
    args = parser.parse_args()
    release(args.app.resolve(), args.out.resolve(), args.identity, args.notary_profile)
