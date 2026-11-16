# Veqtor Workspace

Ask your AI assistant to read, edit and save the documents on your Mac. Point to your
working Word file, include supporting documents for context, and describe the result
you want. Ask for tracked changes, comments or a clean copy. Open the result in Word.

## Install on Mac with Apple Silicon

**[Download Veqtor Workspace for Claude Desktop](https://github.com/JohnDeer-ai/veqtor-workspace/releases/download/v0.1.1/veqtor-workspace-0.1.1-macos-arm64.mcpb)**

1. Install and sign in to [Claude Desktop](https://claude.com/download).
2. Open the downloaded `.mcpb` file with Claude Desktop and choose **Install**.
3. Enter the name you want used for new comments and tracked changes, then enable
   Veqtor Workspace. Start a new chat.

No terminal commands or separate Python installation are needed. Claude downloads
the required runtime and libraries on the first start; allow that step to finish.
The public installer supports Apple Silicon Macs (M1 or newer). Intel Macs are not supported by this installer. An organization may restrict
custom extensions; ask its administrator if installation is disabled.

**[Installation guide and ChatGPT Desktop setup](https://veqtor.pro/setup)** ·
[Release notes and checksums](https://github.com/JohnDeer-ai/veqtor-workspace/releases/tag/v0.1.1)

### Try your first document

Save and close a copy of a Word document. In Finder, select it and press **⌥⌘C** to
copy its full location. Paste it into a new chat with this request:

> Use Veqtor Workspace to open the Word file at [paste the file location]. Make the
> wording clearer without changing its meaning. Show your edits as tracked changes
> and save the result as a new file in the same folder. Tell me where it is.

You can also provide supporting documents, such as another agreement, a report or
meeting notes. The assistant reads the files you identify for context.

Veqtor runs locally. Document content returned to your assistant becomes part of
your conversation with that AI service. Track Changes is used when you ask for it.
Save your Word changes before starting, and avoid editing the same file while the
assistant works. Unsaved Word changes cannot be detected. Review the result in Word.

## ChatGPT Desktop and other local MCP clients

The `.mcpb` installer is for Claude Desktop. For ChatGPT Desktop with local work
enabled, or Codex CLI/IDE, download and unzip the
[v0.1.1 source](https://github.com/JohnDeer-ai/veqtor-workspace/archive/refs/tags/v0.1.1.zip)
to a permanent folder and install [uv](https://docs.astral.sh/uv/getting-started/installation/).
Add a local STDIO server using this command (replace the folder and your name):

Before adding the connection, prepare the runtime once in Terminal:

```sh
uv sync --frozen --no-dev --project /absolute/path/to/veqtor-workspace
```

Then use this launch command:

```sh
uv run --frozen --no-dev --project /absolute/path/to/veqtor-workspace veqtor-workspace \
  --author 'Your Name'
```

Use that same command and arguments as a separate local stdio MCP entry in your
client. An optional `--python /absolute/path/to/python` selects a different document
runtime with the required libraries. `uv run` provisions the default environment.
This command does not change other client connections. Local STDIO connections
require a desktop/local client; this is not a remote server for ChatGPT in a browser.
For ChatGPT Desktop/Codex, set `startup_timeout_sec = 180` and
`tool_timeout_sec = 330` in this server's section of `~/.codex/config.toml`.
These allow the local connection to start and document calls to finish within
Veqtor's 270-second ceiling. See the [official connection settings](https://learn.chatgpt.com/docs/extend/mcp).

## Native ChatGPT installer development

A native Mac installer is being prepared for ChatGPT Desktop: confirm the name for
Word changes, click **Connect to ChatGPT**, then restart ChatGPT. It prepares the
runtime and adds only the Workspace connection, keeping a backup of existing
settings. The public download is pending Developer ID signing and Apple
notarization; the manual instructions above remain the available installation path.
Build and verification instructions are in [the release guide](packaging/RELEASING.md).

## How the workspace works

The connected model writes Python for the selected documents. Four MCP tools:
`workspace_info`, `document_task`, `run_python`, `check_docx`. No embedded model,
account service or negotiation database. The model chooses how to edit and verify
the result. Reading/edit helpers, diagnostics and preview are optional; saving
never requires a clean diagnostic report.

Keep documents and generated artifacts outside Git. Use copies for initial testing.
The connection has no document-folder setting: the model supplies absolute paths
from each user request. macOS permissions still apply.

Ask the model to read `workspace_info`, perform the requested changes, verify the result
by a suitable method and save it. The server snapshots the latest saved version at
`document_task(start, file="/absolute/path")`, keeps the editable copy in private state and replaces
the selected file at `document_task(save)`. Add `file="/absolute/path/new-name.docx"`
to save under a new name in an existing folder. This creates a new file without replacing
the source; an existing destination is never overwritten. The new output is based on the
task snapshot, even if the source was subsequently changed or moved.
Save replies include a `read_hint`: read the resulting path with `run_python(read_paths=[...], code=...)`
without `task_id`. No new editing task is needed just to inspect the saved file.
No manual copying into inputs/ is needed.
Optional `read_paths` on start selects supporting files/folders. `run_python(read_paths=[...])`
can read selected material without creating an editing task; additional grants last only
that call. `scratch_dir` identifies the existing writable `work/` folder for intermediate
extracts, even without an editing task; Python `tempfile` uses `work/.tmp/`. A task has its own scratch folder.
Private copies, previews and logs of other tasks are not readable by Python,
even when a selected parent contains the private storage. A task keeps access to its own
copies and logs. Moving supporting material does not block checking or editing these
copies; a later attempt to read the moved material fails normally. No shared current folder
is switched. The model starts one task per new user editing request, not per Python call; MCP itself
does not receive chat boundaries. `workspace_info` includes [the editing recipe](veqtor_workspace/RECIPE.md)
as `recipe_text`; a separate file-reading call is unnecessary. Detailed examples are in
[the optional reference](veqtor_workspace/REFERENCE.md), returned as a path in `reference`,
not injected into every initial reply.

## Scope

`veqtor_workspace.reading.read_docx(path, query=None, details=False, context=1, format="text")`
is an optional, read-only Python helper for the existing `run_python` tool. Print its compact text
to read projected paragraphs, comments with anchors/replies and optional retained revision evidence.
Search returns matching paragraphs before neighboring context; without a query, reading stays in document order.
There is no helper pagination, paragraph-length cap or maximum neighborhood size.
Paragraphs with retained revision marks show `[rN]`; `details=True` expands their history.
Repeated warnings are grouped by paragraph address. `format="data"` returns full structured
items for custom code; the default text is not JSON. Read the runner's stored output using
`output_cursor` if it exceeds one reply, without reparsing the document. The text incorporates
pending edits for reading only; it is not legal approval or reconstruction of missing
negotiation rounds. Tables use physical XML row/cell positions. Fields, numbering,
drawings and complex structural changes have explicit coverage limits. Custom Python
remains available. The same paragraph reader supplies check_docx's five before/after
excerpts (`changes`, paged with `changes_start`) from the current task's snapshot.
`preview_guidance` locates nested deletions which LibreOffice may display incorrectly and
includes the proposed text of up to five affected paragraphs (new/changed passages first; 900 characters each, with
explicit continuation limits). Read further with `read_docx`; this view does not accept
revisions, create a clean copy or reproduce Word layout. `native_word` separately states
that the server has not checked the file in Word, regardless of rendering success.
Excerpt ordering uses paragraph-sequence comparison, including repeated occurrences;
placement of indistinguishable copies can be ambiguous.

- macOS only. The Python process and children run under an OS sandbox: selected
  file/folder and runtime reads, writes inside task `work/`, no network. The server retains logs.
  The runner does not inherit account credentials or the user's HOME environment.
- Selected documents and `inputs/` snapshots are read-only for task code. The model edits
  a hidden task copy; only `document_task(save)` publishes it to the selected path.
  This works for any file format; DOCX has an additional checker. A prompt cannot
  override macOS restrictions. In path mode the model selects the required read paths
  and target file from user instructions; document contents do not authorize access.
- Optionally save through `veqtor_workspace.files.atomic_save`: write and validate its temporary
  path before leaving the context. Only successful completion replaces the destination;
  a failed write leaves the old file intact. This optional helper protects task-copy
  writes; the final project-file replacement always uses it. A killed process may leave
  a hidden temporary file. It does not validate document content or undo a completed save.
- Saving records the destination and intended result hash before publishing. If the
  final status write fails or the call stops after replacement, the next status read
  recovers the result by comparing hashes at that destination. This also covers save-as.
  Editing is closed once saving begins; an interrupted save cannot be redirected silently.
  `document_task(action="status", task_id=...)` inspects the outcome without saving:
  `saved`, `saved_then_changed`, `not_saved`, or `outcome_unknown`. A newer external
  version is never overwritten merely to resolve an uncertain earlier save.
- Each new task uses the current on-disk file as its baseline, including the user's
  intervening Word edits. Save Word before starting; unsaved edits cannot be read.
  Save compares the project file with the starting snapshot and refuses a detected
  conflict. This is not a live Word integration or an atomic compare-and-swap with
  other apps: do not edit the same file simultaneously while the model saves it.
  If Word stayed open during that save, reopen the file before continuing manual edits
  so a stale in-memory Word version cannot overwrite the model's result.
  Snapshots remain in private application state (`~/Library/Application Support/Veqtor Workspace/`
  by default, `.runs/documents/` below it); no background cleanup or version database.
- Existing `--project` and `--workspace` launch arguments preserve their folder-bound
  behavior. Remove them to use path mode. `--state-dir` changes private storage, not the
  document scope; `--preview-config` overrides the shared renderer configuration.
  Old snapshots remain where they were; start new tasks from the latest saved documents.
- Edits should mark only the text that actually changes, leaving identical surrounding
  text outside Track Changes. Explicit requests to accept/reject revisions or edit/delete
  comments are allowed; the conservative preservation checker needs interpretation for them.
  `veqtor_workspace.text_edits.minimal_edits(before, after)` plans word/number/punctuation
  spans in original Python character offsets; the model maps these to the concrete XML.
  It separates search context from actual change spans without adding an editing engine.
- Python processes start fresh; files persist. One call runs at a time; busy calls
  fail immediately and are never queued. Every MCP call runs in a temporary process
  group with a 270-second ceiling, including checks, rendering, snapshots and saving.
  Received cancellation, server deadline and server-process death stop this group
  and its ordinary children. Cancellation cleanup drains stdout/stderr after stopping
  the group and has a separate two-second ceiling, avoiding a full-pipe wait deadlock. This is a per-call worker, not a persistent job service.
  Python defaults to 255 seconds with immediate output flushing, leaving 15 seconds
  for returning the result. Smaller configured ceilings reserve 10% (at most 15 seconds).
  The model may shorten the Python/preview timer but cannot disable it or exceed the
  connection ceiling. Set `--call-timeout-seconds` below the client's waiting limit,
  leaving time for cleanup and response delivery (for example 270 vs 330 seconds).
  A client that silently stops waiting without cancelling cannot be detected; the
  server ceiling remains the fallback. A response lost after a successful save does
  not undo it: inspect status before assuming success or failure. There is no
  written-file size cap. Actual disk space, memory,
  libraries and model context still bound practical workloads. A shared output-text/artifact-name budget
  defaults to 24,000 UTF-8 bytes. `reply_bytes` can request smaller pages; larger requests are
  capped to the server's `--max-reply-bytes` ceiling. Raise that ceiling only after testing
  actual client delivery, not from advertised model context capacity. Full logs and
  scripts remain in `.runs/` and are read-only to task code. Truncated replies include
  `stdout_next_cursor`, `stderr_next_cursor` and/or `artifacts_next_cursor`, remaining byte counts and `output_hint`.
  Call `run_python(output_cursor=...)` alone to read the stored output; follow `next_cursor` until null.
  No Python code runs again. UTF-8 characters stay intact across page boundaries, and retrying a cursor is idempotent.
  The supervisor stops the call's process group before capturing pages; continuation stays within that captured byte prefix.
  Cursors are signed for this server connection and expire at restart; they can also read a completed task's output.
  This grants no general access to private logs. Continuation tokens are 62-character authenticated
  addresses bound to the connection. Human-readable replies visibly mark each incomplete stream;
  structuredContent retains raw text for exact reconstruction. Status fields are small overhead outside
  the output-text budget. All visible work files are compared before selecting the changed ones.
  Artifact names use at most one quarter of that same budget, leaving room for Python's text/errors;
  artifacts_count and artifacts_remaining make a shortened list explicit. Remaining names continue as JSONL
  through the same output_cursor, also after saving a task. Raw stdout/stderr are not mixed with filenames.
- Each Python call receives `VEQTOR_UTC_NOW`, an actual UTC timestamp, alongside
  `VEQTOR_AUTHOR`. These are conveniences for new revisions/comments, not enforced
  metadata. Diagnostics check the original timezone spelling/range (without normalizing
  invalid offsets) and warn about missing/invalid/timezone-less dates or dates more
  than five minutes ahead of `checked_at_utc`. Retained historical dates are not changed
  or rechecked as new. Cached reports retain their original check time; saving is unaffected.
- The checker undoes the new tracked changes and new comments and compares the whole
  document with the original, so untracked text, formatting, paragraph, table and
  comment mismatches are reported with their location. It is an optional diagnostic,
  not a mandatory step, a full difference report or an approval verdict. The summary has
  `errors` (the comparison could not read the package/XML), `observations` (comparison
  mismatches or revision metadata/structure notices), `warnings` and `not_checked`.
  Passed checks keep only their status and useful counters or metadata; explanations
  accompany findings. Unchecked areas remain explicit even when no findings are present.
  Observations need interpretation against the user's task: authorized acceptance,
  rejection, comment deletion and clean copies may intentionally change the baseline.
  Ambiguous old/new metadata can also produce false observations. Concrete unexpected
  losses and compatibility problems still warrant investigation; a clean report is not
  required before saving. The checker does not open Word, validate XSD/relationships, or certify legal
  wording, layout or fields. Its separate resource limit is 64 MiB of expanded ZIP
  content and 5,000 members: larger packages return `not_checked`, not a corruption
  finding. This does not prevent Python from reading or editing those files.
- `check_docx(render=True)` optionally uses an existing renderer to preview the original
  and candidate within one shared deadline: 60 seconds by default, configurable with
  `preview_timeout_seconds` within the connection's whole-call ceiling. Converter children are stopped on
  timeout; there are no automatic retries. It reports page counts and differing PNGs,
  renders every page and returns up to three images per reply. To inspect more pages,
  reuse the returned `preview_id` with `preview_pages=[4, 5, 6]`; set `preview_side="original"`
  to compare original pages. This reuses the images without another conversion and refuses
  stale previews if either document changed. Inspect every relevant changed page; three
  images are not a complete review of a large edit. This does not certify layout or test
  native Word comments/accept/reject. The same rendering adapter is callable without MCP.
  `page_numbering` explicitly labels all counts and page lists as preview pages; Word
  may paginate differently. Locate changes by clause number or quoted text as well.
  `preview_query="distinctive phrase"` searches the saved preview PDF, reports matching
  pages and shows up to three of them. It does not reconvert; no OCR or cross-page phrase
  matching is included. `changed_pages` denotes visual differences, including page reflow.
  Later page/search calls reuse a check from this version only when both file hashes and
  author still match, returning a compact summary. Re-render after any final edit.
- `redline_quality` is a non-blocking observation of unchanged word edges in adjacent
  new plain-text deletions/insertions with identical run properties. It skips complex
  history, moves, fields, formatting changes and groups over 20,000 characters per side;
  it does not certify minimality or auto-rewrite the document. Review its named fragments.
- Existing-revision/comment preservation is a conservative baseline. Intentional
  acceptance, rejection or deletion requires interpretation of the report.
  When old split pieces and new insertions have identical author/time metadata,
  their origins remain uncertain. An interpretation is used only if undoing it exactly
  restores the entire story and history; no paragraphs are excluded from comparison.
  Split run-formatting revisions can receive fresh ids while their author, date,
  properties and text coverage are preserved. Native Word acceptance of one text edit does not prove
  that saving in Word preserved all earlier formatting revisions.
- This is a single-user development experiment, not a reviewed execution-security
  product. Test the concrete access restrictions on the target machine.

The code executor is format-independent. DOCX editing and text-PDF reading are tested.
`pypdf` is included so both clients can extract text from supporting PDFs in the workspace.
Image-only PDFs need OCR; OCR, PDF visual review and other Office-format libraries/checks
are not bundled. A custom `--python` runtime may provide additional libraries.

## Optional preview setup

The installed Codex document renderer is discovered automatically on this Mac, including
from the Claude extension. A missing version-specific Codex script falls back to the
runtime path without a version number, then to the latest installed document plugin.
`workspace_info` checks the Python executable and script; rendering still reports conversion failures.

To use a custom renderer, configure
`~/Library/Application Support/Veqtor Workspace/preview.json`, outside writable task folders.
Valid custom paths are preserved; a missing custom script is reported, never silently replaced.
An explicit `--preview-config` can select another config; legacy local `.preview.json` remains supported:

```json
{"python": "/absolute/path/to/render-python", "script": "/absolute/path/to/render_docx.py"}
```

Veqtor does not install or bundle an office suite or rewrite renderer configuration.
Without a usable renderer, previews explicitly report `not_available` and editing still works.
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
older versions, the saved document-folder preference is no longer used. The only
user setting is the revision/comment author. Document paths come from each request.
Private state and the shared renderer configuration stay outside this repository.
