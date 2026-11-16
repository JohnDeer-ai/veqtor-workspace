# Public Mac release

1. Update `pyproject.toml`, `veqtor_workspace/__init__.py`, the manifest and the
   versioned README links. Refresh `uv.lock` with `uv lock`.
2. Run `uv run --frozen --group installer pytest -q`, build with
   `uv run --frozen python packaging/build_mcpb.py --out dist`, and validate the
   manifest with `npx --yes @anthropic-ai/mcpb@2.1.2 validate packaging/manifest.json`.
3. Run `packaging/smoke_mcpb.py` through `uv run --frozen python`, passing the built
   extension. It downloads a fresh runtime and tests actual MCP calls using only
   disposable documents. It does not install anything into a desktop client.
4. Require green Workspace CI on Apple Silicon. The fresh-runtime smoke must install all
   dependencies with `--no-build`; a CI compiler must not mask a missing wheel.
   Compare the uploaded installer hash with the local package. Never build public assets
   with `--default-author`.
5. Tag the tested commit, attach the tested `.mcpb` and `SHA256SUMS` to its GitHub
   release, and verify a fresh download against the checksum. Update the version,
   URL and checksum on the website together.

The extension keeps the internal ID `veqtor-workspace-pilot` to update existing
installations. Its public name and asset filename are Veqtor Workspace. The
`.mcpb` package is a Claude Desktop extension, not a system installer or a
ChatGPT Desktop extension. Record protocol checks and any native-app checks
separately; a successful smoke test does not establish visual Word acceptance.

## Native ChatGPT Desktop installer

This is a separate native app for the ChatGPT Desktop local-work configuration
(`~/.codex/config.toml`), not a `.mcpb` file or a browser connector. It requires
macOS 13 or newer on Apple Silicon. It bundles a checksum-pinned uv binary, then
downloads managed Python and frozen dependencies without compilers. No developer
tools are needed on the user's Mac.

Build and test without touching the real client:

```sh
uv run --frozen python packaging/build_chatgpt_app.py --out dist/chatgpt-candidate
uv run --frozen python packaging/smoke_chatgpt_app.py \
  'dist/chatgpt-candidate/Veqtor Workspace Setup.app' \
  dist/chatgpt-candidate/verify-installer
```

The verification driver uses the same Swift engine and app resources in a fresh,
disposable profile. The smoke test then uses the resulting connection for tracked
Word edits, comments, save-as, in-place saves, and saved-source conflict detection.
Open the app for a separate visual check, without connecting a real client during
automated tests. A protocol test is not evidence that the ChatGPT UI has loaded
the connection; record these checks separately.

The local candidate is ad-hoc signed for development. **Do not publish it as a
normal Mac installer.** An Apple Personal Team / Apple Development certificate
cannot sign public outside-App-Store distribution. A paid Apple Developer Program
membership, Developer ID Application certificate and Apple notarization are
required for the intended public download.

Once the distribution identity and a `notarytool` keychain profile already exist:

```sh
uv run --frozen python packaging/release_chatgpt_app.py \
  --app 'dist/chatgpt-candidate/Veqtor Workspace Setup.app' \
  --out dist/public-chatgpt \
  --identity 'Developer ID Application: YOUR NAME (TEAMID)' \
  --notary-profile YOUR_EXISTING_KEYCHAIN_PROFILE
```

The release script signs the bundled helper and app with the hardened runtime,
creates a signed disk image, submits it to Apple, requires `Accepted`, staples the
ticket, and verifies Gatekeeper acceptance. It does not create credentials or
accept passwords. Only after this succeeds should the tested `.dmg`, `.mcpb` and
checksums be attached to the same tagged release and linked from `/setup`.

Installation creates a separate runtime under
`~/Library/Application Support/Veqtor Workspace/ChatGPT/releases/`. It checks the
four MCP tools and version before changing the client's settings. Existing
connections, comments, model preferences and tool approval settings are preserved;
a private `config.toml.before-veqtor-*` backup is saved before replacement. An
existing disabled Workspace connection stays disabled. The app refuses ambiguous,
remote or symlinked configurations and settings it sees changed while installing.
It does not coordinate writes with the ChatGPT process, so users should avoid
editing connection settings during installation. It never closes/restarts ChatGPT
or opens the user's documents. After installation, restart ChatGPT and start a new
chat. Reinstallation keeps old runtimes so an existing chat is not disrupted.

To disconnect, remove/disable Veqtor Workspace in ChatGPT's MCP settings. After
closing chats that use it, the `ChatGPT` subfolder above can be removed to reclaim
installed runtimes and download caches. Do not delete the parent `Veqtor Workspace`
folder: it may contain private document snapshots used by other connections.

References: [ChatGPT local MCP settings](https://learn.chatgpt.com/docs/extend/mcp)
and [Apple Developer ID distribution](https://developer.apple.com/developer-id/).
