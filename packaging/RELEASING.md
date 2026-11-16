# Public Mac release

1. Update `pyproject.toml`, `veqtor_workspace/__init__.py`, the manifest and the
   versioned README links. Refresh `uv.lock` with `uv lock`.
2. Run `uv run --frozen pytest -q`, build with
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
