# SPDX-License-Identifier: Apache-2.0
"""Package the native app as a disk image. Unsigned distribution is explicit."""
import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path


def package(app: Path, out: Path, *, unsigned: bool) -> Path:
    metadata = json.loads((app / "Contents/Resources/release.json").read_text())
    version = metadata["version"]
    out.mkdir(parents=True, exist_ok=True)
    dmg = out / f"veqtor-workspace-{version}-chatgpt-macos-arm64.dmg"
    if dmg.exists():
        raise FileExistsError(dmg)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    if not unsigned:
        signing = subprocess.run(["codesign", "--display", "--verbose=4", str(app)],
                                 check=True, capture_output=True, text=True)
        if "Authority=Developer ID Application:" not in signing.stderr:
            raise ValueError("No Developer ID signature. Unsigned packaging requires --unsigned.")
    notice = (
        "This installer is not signed with Developer ID and has not been notarized by Apple.\n"
        "After the first blocked attempt to open it, go to System Settings > Privacy & Security,\n"
        "find Veqtor Workspace Setup, choose Open Anyway and confirm Open.\n"
        "Only allow the installer you downloaded from the official Veqtor release.\n"
        "Apple's explanation: https://support.apple.com/en-us/102445\n\n"
    ) if unsigned else ""
    with tempfile.TemporaryDirectory(prefix="veqtor-dmg-") as temporary:
        staging = Path(temporary) / "Veqtor Workspace"
        staging.mkdir()
        subprocess.run(["ditto", str(app), str(staging / app.name)], check=True)
        (staging / "Read me.txt").write_text(
            "Veqtor Workspace for ChatGPT Desktop\n\n" + notice +
            "1. Open Veqtor Workspace Setup.\n"
            "2. Confirm your name for Word changes and click Connect to ChatGPT.\n"
            "3. Restart ChatGPT Desktop and start a new chat.\n\n"
            "Mac with M1 or newer, macOS 13 or newer, and ChatGPT Desktop with local work enabled.\n"
            "Internet is required during installation.\n"
            "Guide: https://veqtor.pro/setup\n"
            "Source: https://github.com/JohnDeer-ai/veqtor-workspace\n"
        )
        subprocess.run(["hdiutil", "create", "-format", "UDZO", "-volname", "Veqtor Workspace",
                        "-srcfolder", str(staging), str(dmg)], check=True)
    return dmg


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--unsigned", action="store_true", required=True,
                        help="Explicitly package without Developer ID/notarization and include the macOS permission instructions")
    args = parser.parse_args()
    dmg = package(args.app.resolve(), args.out.resolve(), unsigned=args.unsigned)
    print(json.dumps({"artifact": str(dmg), "sha256": hashlib.sha256(dmg.read_bytes()).hexdigest(),
                      "developer_id_signed": False, "notarized": False}))
