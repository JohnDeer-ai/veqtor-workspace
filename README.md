# Veqtor Workspace

An independent local document workspace, extracted from the Veqtor pilot. The connected model
writes Python for the selected documents. Four MCP tools: `workspace_info`,
`document_task`, `run_python`, `check_docx`. No embedded model, account service or negotiation database.

## Try on macOS

Select a document project folder outside the repository. Keep contracts and generated
artifacts outside Git. Use a folder of copies for initial testing.

```sh
uv run --project /absolute/path/to/veqtor-workspace veqtor-workspace \
  --project /absolute/path/to/document-folder --author 'Your Name'
```

Use that same command and arguments as a separate local stdio MCP entry in your
client. An optional `--python /absolute/path/to/python` selects a different document
runtime with the required libraries. `uv run` provisions the default environment.
No existing Veqtor installation or client configuration is changed by this prototype.

Ask the model to read `workspace_info`, edit the selected file with genuine tracked
changes, check it and save it back. The server snapshots the latest saved version at
`document_task(start)`, keeps the editable copy in hidden `.veqtor/` state and replaces
the selected file at `document_task(save)`. No manual copying into inputs/ is needed.
The model starts one task per new user editing request, not per Python call; MCP itself
does not receive chat boundaries. `workspace_info` includes [the editing recipe](veqtor_workspace/RECIPE.md)
as `recipe_text`; a separate file-reading call is unnecessary.

## Scope

- macOS only. The Python process and children run under an OS sandbox: selected
  project/runtime reads, writes inside task `work/`, no network. The server retains logs.
  The runner does not inherit account credentials or the user's HOME environment.
- Project documents and `inputs/` snapshots are read-only for task code. The model edits
  a hidden task copy; only `document_task(save)` publishes it to the selected path.
  This works for any file format; DOCX has an additional checker. A prompt cannot
  override filesystem restrictions or grant access outside the selected project.
- Save through `veqtor_workspace.files.atomic_save`: write and validate its temporary
  path before leaving the context. Only successful completion replaces the destination;
  a failed write leaves the old file intact. This optional helper protects task-copy
  writes; the final project-file replacement always uses it. A killed process may leave
  a hidden temporary file. It does not validate document content or undo a completed save.
- Each new task uses the current on-disk file as its baseline, including the user's
  intervening Word edits. Save Word before starting; unsaved edits cannot be read.
  Save compares the project file with the starting snapshot and refuses a detected
  conflict. This is not a live Word integration or an atomic compare-and-swap with
  other apps: do not edit the same file simultaneously while the model saves it.
  If Word stayed open during that save, reopen the file before continuing manual edits
  so a stale in-memory Word version cannot overwrite the model's result.
  Snapshots remain in `.veqtor/.runs/documents/`; no background cleanup or version database.
- `--workspace` still supports the old inputs/work copy workflow. It does not provide
  automatic snapshots of external files. Existing client entries keep working until
  changed to `--project`; the 0.0.5 Claude bundle uses project mode.
- Edits should mark only the text that actually changes, leaving identical surrounding
  text outside Track Changes. Explicit requests to accept/reject revisions or edit/delete
  comments are allowed; the conservative preservation checker needs interpretation for them.
- Python processes start fresh; files persist. Calls run sequentially. The default
  deadline is 300 seconds, with no hard maximum; `timeout_seconds=null` disables the
  server deadline. Client-side MCP timeouts are separate (configure a longer tool
  timeout for long calls). There is no written-file size cap. Actual disk space, memory,
  libraries and model context still bound practical workloads. Output replies are bounded; full logs and
  scripts remain in `.runs/` and are read-only to task code.
- The checker undoes the new tracked changes and new comments and compares the whole
  document with the original, so untracked text, formatting, paragraph, table and
  comment changes are reported with their location. Its summary separates defects,
  compatibility warnings and checks not performed: structures that Word opens only as a
  repaired copy are defects, placements Word handles but does not write itself are
  warnings. It does not open Microsoft Word, validate XSD/relationships, or certify legal
  wording, layout or fields. Its separate resource limit is 64 MiB of expanded ZIP
  content and 5,000 members: larger packages return `not_checked`, not a corruption
  finding. This does not prevent Python from reading or editing those files.
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

The code executor is format-independent. DOCX editing and text-PDF reading are tested.
`pypdf` is included so both clients can extract text from supporting PDFs in the workspace.
Image-only PDFs need OCR; OCR, PDF visual review and other Office-format libraries/checks
are not bundled. A custom `--python` runtime may provide additional libraries.

## Optional preview setup

An administrator configures an already installed renderer once in the project's
`.veqtor/.preview.json` (or `.preview.json` in a legacy workspace, outside writable `work/`):

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
an update replaces the pilot instead of creating a second connection. On update from
0.0.4, choose the folder of actual documents in extension settings (the stored setting
key remains `workspace` for compatibility). Document workspaces
and machine-specific preview configuration stay outside this repository.
