# Optional reference

Consult only the section relevant to the task. These are examples and known limits,
not a required sequence or a closed set of editing operations.

## Reading and precise text edits

`from veqtor_workspace.reading import read_docx`
`print(read_docx(path, query="distinctive phrase", details=True, context=5))` returns compact
text with retained revisions, comments/replies and table-cell context. Omit query to read
the whole document. context accepts any nonnegative number of neighboring XML paragraphs.
There is no helper page or element limit. The runner saves its entire printed output;
use run_python's output_cursor if it exceeds one reply. Search includes deleted text and comments.
Use format="data" for a dictionary with full `items`, original paragraph IDs, locations,
comment anchors and metadata. The helper does not shorten paragraphs or rewrite the file.
Default text groups repeated warnings by their paragraph IDs, not by a generated summary.
Matching paragraphs come before neighboring context; both remain available through continuation.
Projected current text incorporates pending edits for reading; it does not accept them in
the file or reconstruct rounds whose markup has been removed. Inspect reported coverage
limits with custom code or Word when relevant.

`from veqtor_workspace.text_edits import minimal_edits`
`minimal_edits("within 30 days", "within 45 days")` returns only the number replacement.
`minimal_edits("это необходимая", "— это необходимая")` returns only a punctuation insertion.
Offsets are Python characters in the original text. Apply spans in reverse offset order
or rebuild once. The helper plans text changes; it does not edit XML or choose legal wording.
Use whole words/numbers for readable redlines, with character edits when appropriate.
A substantially rewritten phrase can use a whole-phrase replacement. Preserve word boundaries.

Text PDFs can be read with the bundled `pypdf`. Image-only scans need OCR, which is not bundled.
For intermediate extracts, write `Path('work/extract.txt')` or use Python `tempfile`.
`scratch_dir` gives the exact writable folder, including in reading-only calls; the cwd root and `/tmp` are not writable.
Files in `work/` persist between calls in that context; an editing task has its own `work/`.
If Python output or artifact names are truncated, call `run_python(output_cursor=...)` using
`stdout_next_cursor`, `stderr_next_cursor` or `artifacts_next_cursor`,
without code, task_id or read_paths. Each page returns stored `text`, byte counts and `next_cursor` (null at the end).
Reusing a cursor rereads the same page; it never executes code. Cursors expire when the server connection restarts.
This also works after a task is saved. Other private logs remain inaccessible.
reply_bytes selects a page within the server ceiling (normally 24,000 UTF-8 bytes shared by stdout/stderr/artifact names).
Artifact continuations are JSONL filenames; join the returned text before decoding complete lines.
The initial artifacts list is a prefix, with artifacts_count and artifacts_remaining showing its completeness.
Oversized requests are capped, not rejected. Human-readable replies mark continuation next to the output;
structuredContent keeps raw stdout/stderr and continuation text unchanged for programmatic use.

## Direct OOXML editing examples

`zipfile` and `lxml` can change selected DOCX parts while copying other ZIP members unchanged.
Assigning `paragraph.text` to an existing redlined paragraph can erase history and formatting.
These examples apply when choosing direct XML editing; another valid implementation is allowed.

Use the configured author (`os.environ["VEQTOR_AUTHOR"]`) and fresh unique ids.
After attaching a new revision to the document, optionally call
`veqtor_workspace.text_edits.stamp_revision_time(element)`. It uses the Mac time and offset
supplied as `VEQTOR_LOCAL_NOW` for each Python call, writing local clock time in `w:date`
and the UTC instant in `w16du:dateUtc`. `VEQTOR_UTC_NOW` is available for comments.
For new revisions with a UTC companion, diagnostics use it to flag instants more than five minutes
ahead of `checked_at_utc` and check that a numeric offset in `w:date` gives the same instant.
Other revision dates and comment dates require an explicit timezone
(`Z` or `+/-HH:MM`, with minutes 00–59 and a maximum offset of 14:00).
Preserve unrelated earlier text, authors, dates and history.
When splitting runs, preserve mixed formatting and use `xml:space="preserve"` on text pieces
with leading/trailing spaces. Word otherwise drops those spaces.

- Replace text: wrap the old runs in `w:del` with `w:delText`, followed by `w:ins` for the new text.
- Counter a pending insertion: put the new deletion inside the earlier insertion around the
  struck runs. Put the replacement insertion beside that old insertion. Split the earlier
  insertion as necessary, preserving its author/date and assigning fresh ids to split pieces.
- Restore earlier deleted text: retain its old deletion and add a new insertion beside it.
- New paragraph before another: mark its paragraph mark (`w:pPr/w:rPr/w:ins`) and its runs.
  At the end of a document or table cell, mark the preceding paragraph mark instead.
- Delete a paragraph: mark both its runs and its paragraph mark as deleted.
- New table row: put `w:ins` in `w:trPr` and wrap its text runs in insertions.
- Record old formatting in `w:rPrChange` / `w:pPrChange`. If splitting a run carrying an
  earlier formatting revision, give additional copies unique ids, retaining author/date,
  recorded properties and the original text coverage. The copies are not a new change by you.
- A comment needs its comment entry, range anchors/reference, relationship and content type.

Some nested revision forms need Word repair or differ between renderers. A successful XML
parse or preview does not establish native Word behavior. Do not change valid history merely
to satisfy a preview renderer or a diagnostic heuristic. Requested acceptance/rejection and
comment removal intentionally change the baseline history.

## Optional diagnostics and preview

`check_docx(task_id=...)` compares that task's snapshot and result. In path mode,
`check_docx(original="/received/file.docx", candidate="/draft/file.docx")` directly compares
two selected files outside the server's private workspace, including files with the same name.
Within the private workspace, both absolute and relative paths must use `inputs/` for
`original` and `work/` for `candidate`. A direct comparison of two workspace copies
requires one in each directory. `comparison` identifies the file pair.
The diagnostics assume additive tracked edits: they identify and undo new revisions/comments, then compare.
It is not a complete difference report and cannot always separate old/new revisions sharing
metadata. `observations` may describe requested changes or uncertain history; interpret them
against the user's task. `errors` means the comparison could not read the package/XML;
inspect the reason before concluding either file is damaged. Warnings and unchecked areas
remain explicit. Neither calling this tool nor clearing its observations is required to save.
Do not dismiss a concrete unexpected loss just because the diagnostic is optional.

`changes` lists five before/after paragraph excerpts and their addresses before the preview pages.
Pass `next_start` as `changes_start` for more excerpts. The optional
`redline_quality` observation covers only simple adjacent replacements, not all possible edits.

`check_docx(task_id=..., render=True)` renders both copies with a shared deadline (60 seconds
by default; set `preview_timeout_seconds` within the connection ceiling when needed).
All pages are rendered; at most three images are returned per reply. Reuse `preview_id`
with `preview_pages=[...]` or `preview_query="distinctive phrase"`; `preview_side="original"`
selects the baseline. Preview reuse checks the selected paths and bytes. Re-render after later edits
or when a path-mode preview predates path recording.
Pages belong to the preview, not Word pagination. Include clause numbers or text as locators.
`changed_pages` includes shifts caused by pagination, not just edited clauses.
Search has no OCR or cross-page phrase matching; no match does not prove absence from the DOCX.

Inspect relevant output to the extent needed for the task. Preview is optional and may time out.
`preview_guidance` locates nested deletions in the candidate and shows proposed text for up
to five affected paragraphs (new/changed passages first; 900 characters each, explicitly marked if incomplete).
Ordering uses the existing paragraph-sequence comparison, including repeated occurrences;
placement of indistinguishable copies can be ambiguous.
Use `read_docx` for full text/more passages; this does not accept revisions.
LibreOffice may omit strikethrough for these deletions. Text projection uses pending insertions
and excludes deletions from all retained rounds; fields, objects and table layout have stated limits.
`native_word="not_checked_by_server"` is separate from rendering/diagnostics. Report any external
Word opening, display and accept/reject checks separately. Cached date checks retain `checked_at_utc`.

## Saving and interrupted calls

For candidate writes, `from veqtor_workspace.files import atomic_save` provides:
`with atomic_save(candidate_path) as draft:` — write, close and validate `draft` inside the
block. Only successful completion replaces the candidate. `overwrite=False` creates a new
file only if the destination is still unused. This helper is optional; it does not validate
document contents. A completed replacement is not rolled back by a later script error.

`document_task(save)` publishes the task's candidate, never arbitrary additional work files.
Without `file` it replaces the source only when it still matches the snapshot.
With a new `file` path it creates an output without replacing the source or any existing file.
The destination folder must already exist. A saved task is closed; start a new task for later edits.
To read the current saved file, use `run_python(read_paths=[saved_path], code=...)` without `task_id`.
Save/status replies include this `read_hint` with the exact destination; reading needs no new task.
After any interrupted save, `document_task(status)` reports the recorded destination:
`saved`, `not_saved`, `saved_then_changed` or `outcome_unknown`.
A save intent closes editing even if the final status write fails. Use status before retrying;
an unknown outcome is not permission to overwrite a newer file.

Calls run one at a time, with no queue. The connection deadline applies per call, not to the
whole user task. Read actual limits from `workspace_info`; Python keeps time for returning
its output. A successful run does not itself validate the document.
