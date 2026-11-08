# Veqtor Workspace

An independent local document workspace, extracted from the Veqtor pilot. The connected model
writes Python for the selected documents. Three MCP tools: `workspace_info`,
`run_python`, `check_docx`. No embedded model, account service or negotiation database.

## Try on macOS

Create a private workspace outside the repository. Put **copies** of selected files
inside its `inputs/` folder. Keep contracts and generated artifacts outside Git.

```sh
uv run --project /absolute/path/to/veqtor-workspace veqtor-workspace \
  --workspace /absolute/path/to/private-pilot --author 'Your Name'
```

Use that same command and arguments as a separate local stdio MCP entry in your
client. An optional `--python /absolute/path/to/python` selects a different document
runtime with the required libraries. `uv run` provisions the default environment.
No existing Veqtor installation or client configuration is changed by this prototype.

Ask the model to read `workspace_info`, inspect a copy, edit it with genuine tracked
changes, then check and review the output. See [the short recipe](veqtor_workspace/RECIPE.md).

## Scope

- macOS only. The Python process and children run under an OS sandbox: selected
  workspace/runtime reads, writes inside `work/` and execution logs, no network.
  The runner does not inherit account credentials or the user's HOME environment.
- `inputs/` copies are read-only for task code. An existing file in `work/` can be edited
  in place when requested. A prompt cannot override filesystem restrictions.
- Edits should mark only the text that actually changes, leaving identical surrounding
  text outside Track Changes. Explicit requests to accept/reject revisions or edit/delete
  comments are allowed; the conservative preservation checker needs interpretation for them.
- Python processes start fresh; files persist. Calls run sequentially, up to 120 seconds.
  Each written file is limited to 8 MB. Output replies are bounded; full logs and
  scripts remain in `.runs/` and are read-only to task code.
- The checker undoes the new tracked changes and new comments and compares the whole
  document with the original, so untracked text, formatting, paragraph, table and
  comment changes are reported with their location. Its summary separates defects,
  compatibility warnings and checks not performed: structures that Word opens only as a
  repaired copy are defects, placements Word handles but does not write itself are
  warnings. It does not open Microsoft Word, validate XSD/relationships, or certify legal
  wording, layout or fields.
- `check_docx(render=True)` optionally uses an existing renderer to preview the original
  and candidate within one shared deadline: 60 seconds by default, configurable up to
  300 with `preview_timeout_seconds`. Converter children are stopped on
  timeout; there are no automatic retries. It reports page counts and differing PNGs,
  renders every page and returns up to three images per reply. To inspect more pages,
  reuse the returned `preview_id` with `preview_pages=[4, 5, 6]`; set `preview_side="original"`
  to compare original pages. This reuses the images without another conversion and refuses
  stale previews if either document changed. Inspect every relevant changed page; three
  images are not a complete review of a large edit. This does not certify layout or test
  native Word comments/accept/reject. The same rendering adapter is callable without MCP.
- Existing-revision/comment preservation is a conservative baseline. Intentional
  acceptance, rejection or deletion requires interpretation of the report.
- This is a single-user development experiment, not a reviewed execution-security
  product. Test the concrete access restrictions on the target machine.

The code executor is format-independent. This pilot exercises DOCX only; other
formats can use the same runner but need appropriate libraries and task checks.

## Optional preview setup

An administrator configures an already installed renderer once in the workspace's
`.preview.json` (outside writable `work/`):

```json
{"python": "/absolute/path/to/render-python", "script": "/absolute/path/to/render_docx.py"}
```

The pilot does not install or bundle an office suite. This machine uses the bundled
Codex document renderer; the same paths also work for the Claude extension on this Mac.
Without it, previews explicitly report `not_available` and editing still works.
The fixed converter runs separately from model-written code, without inherited
credentials or network access and with both input documents protected. It has broader
local read access than task Python so the existing converter and fonts can work.
Task Python's existing restrictions are unchanged. MCP preview files/logs stay in
`.runs/previews/`, read-only to task Python. There is no Word UI automation or background service.

## Verification

Synthetic tests live in `tests/`; real files and results stay private. Compare the
same tasks through direct Python and MCP, and include deliberately damaged copies
to establish what the checks actually detect. Report mechanical, visual and
native-Word checks separately.

Run `uv run --group dev pytest` from this repository.

## Claude Desktop package

Build with `uv run python packaging/build_mcpb.py --out dist` and install the resulting
`.mcpb` extension. The extension keeps its existing `veqtor-workspace-pilot` identity so
an update replaces the pilot instead of creating a second connection. Document workspaces
and machine-specific preview configuration stay outside this repository.
