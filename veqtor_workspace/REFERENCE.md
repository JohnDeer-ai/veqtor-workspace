# Optional reference

Consult only the section relevant to the task. These are examples and known limits,
not a required sequence or a closed set of editing operations.

## Reading and precise text edits

`from veqtor_workspace.reading import read_docx`
`read_docx(path, query="distinctive phrase", details=True)` returns relevant paragraphs,
retained revisions, comments/replies and table-cell context. Follow `next_cursor` with
identical options until None; long passages continue in chunks. A changed file invalidates
the cursor. Use query=None to read sequentially. Search includes deleted text and comments.
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
Print relevant evidence in manageable portions. If truncated, follow `output_hint` to continue
reading; do not repeat edits to recover output. `read_docx` already supports `next_cursor`.

## Direct OOXML editing examples

`zipfile` and `lxml` can change selected DOCX parts while copying other ZIP members unchanged.
Assigning `paragraph.text` to an existing redlined paragraph can erase history and formatting.
These examples apply when choosing direct XML editing; another valid implementation is allowed.

New revisions/comments can use the configured author (`os.environ["VEQTOR_AUTHOR"]`) and
`os.environ["VEQTOR_UTC_NOW"]`, supplied afresh for each Python call, plus fresh unique ids.
Do not label local time with Z; a deliberately specified date remains your choice.
Diagnostics warn about missing/invalid dates, absent timezones and dates over five minutes
ahead of `checked_at_utc`, only for new revisions/comments. They do not rewrite dates or gate saving.
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

`check_docx(task_id=...)` compares the snapshot with the result. The existing comparison
assumes additive tracked edits: it identifies and undoes new revisions/comments, then compares.
It is not a complete difference report and cannot always separate old/new revisions sharing
metadata. `observations` may describe requested changes or uncertain history; interpret them
against the user's task. `errors` means the comparison could not read the package/XML;
inspect the reason before concluding either file is damaged. Warnings and unchecked areas
remain explicit. Neither calling this tool nor clearing its observations is required to save.
Do not dismiss a concrete unexpected loss just because the diagnostic is optional.

`changes` contains five before/after paragraph excerpts from this task. Follow `next_start`
using `changes_start` for more; this does not reconstruct past rounds. The optional
`redline_quality` observation covers only simple adjacent replacements, not all possible edits.

`check_docx(task_id=..., render=True)` renders both copies with a shared deadline (60 seconds
by default; set `preview_timeout_seconds` within the connection ceiling when needed).
All pages are rendered; at most three images are returned per reply. Reuse `preview_id`
with `preview_pages=[...]` or `preview_query="distinctive phrase"`; `preview_side="original"`
selects the baseline. The cache is only reused for unchanged bytes. Re-render after later edits.
Pages belong to the preview, not Word pagination. Include clause numbers or text as locators.
`changed_pages` includes shifts caused by pagination, not just edited clauses.
Search has no OCR or cross-page phrase matching; no match does not prove absence from the DOCX.

Inspect relevant output to the extent needed for the task. Preview is optional and may time out.
`preview_guidance` locates nested deletions in the candidate and shows proposed text for up
to five affected paragraphs (new/changed passages first; 900 characters each, explicitly marked if incomplete).
Use `read_docx` and its cursor for full text/more passages; this does not accept revisions.
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
After any interrupted save, `document_task(status)` reports the recorded destination:
`saved`, `not_saved`, `saved_then_changed` or `outcome_unknown`.
A save intent closes editing even if the final status write fails. Use status before retrying;
an unknown outcome is not permission to overwrite a newer file.

Calls run one at a time, with no queue. The connection deadline applies per call, not to the
whole user task. Read actual limits from `workspace_info`; Python keeps time for returning
its output. A successful run does not itself validate the document.
