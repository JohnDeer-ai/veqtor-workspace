# Optional reference

Choose the examples and checks that fit the task.

## Reading and precise text edits

`from veqtor_workspace.reading import read_docx`
`print(read_docx(path, query="distinctive phrase", details=True, context=5))` shows matches,
nearby XML paragraphs and retained revision details. Search includes deleted text and comments.
Omit the query for the whole document. Use `read_docx(path, table_row="p1423")` to read all
cells in the physical row containing a hit, including nested tables. Use the full ID, for
example `word/header1.xml#p2`, for other document parts. IDs refer to the saved file version.

Text output marks direct Word highlight as `[highlight:yellow]text[/highlight]`, and direct
strikethrough as `[strike]` or `[double-strike]`. With `format="data"`, the text stays searchable
and the optional `highlight_spans` and `strikethrough_spans` use character offsets with an
exclusive end. Highlight colors come from explicit run properties. Read the coverage notes
when fields, numbering, objects or layout matter. Pending changes are projected in the read
view. Acceptance modifies the DOCX, and rounds whose markup is gone cannot be reconstructed.

`from veqtor_workspace.text_edits import minimal_edits`
`minimal_edits("within 30 days", "within 45 days")` plans a number replacement.
Offsets are Python characters in the original text. Apply edits in reverse offset order or
rebuild once. Preserve word boundaries and use whole-word or phrase replacements when useful.

Use the runner's output cursors to recover complete reads. They retrieve stored output for
the current connection, including after a save. Text PDFs can be read with bundled `pypdf`;
image-only scans need separate OCR.

## Accept incoming changes and show only your edits

After accepting the requested revisions, you can save a separate copy of the candidate in
`work/`. Make your edits, then compare the two copies in Python within the same task, for
example with `veqtor_workspace.checks.check_docx`. Use the `inputs/` snapshot to verify
acceptances when needed.

## Direct OOXML editing examples

`zipfile` and `lxml` can change selected parts and copy other ZIP members unchanged.
Assigning `paragraph.text` to an existing redlined paragraph can erase history and formatting.
Preserve unrelated earlier text, authors, dates and history. When splitting runs, preserve
mixed formatting and use `xml:space="preserve"` on text with leading or trailing spaces.

Use `os.environ["VEQTOR_AUTHOR"]` and fresh unique revision IDs. The optional
`veqtor_workspace.text_edits.stamp_revision_time(element)` uses the Mac time and offset to write
`w:date` and the UTC instant in `w16du:dateUtc`. `VEQTOR_UTC_NOW` is available for comments.
Use explicit timezones for dates supplied directly.

- Replace text with a `w:del` containing `w:delText`, followed by `w:ins` for the new text.
- To counter a pending insertion, put your deletion inside the earlier insertion and put the
  replacement beside it. If splitting the earlier insertion, retain its author/date and use fresh IDs for split pieces.
- To restore earlier deleted text, retain the deletion and add an insertion beside it.
- For a new paragraph before an existing one, mark its runs and its paragraph mark
  (`w:pPr/w:rPr/w:ins`). At the end of a document or cell, mark the preceding paragraph mark.
- To delete a paragraph, mark both its runs and its paragraph mark as deleted.
- For a new table row, put `w:ins` in `w:trPr` and wrap its text runs in insertions.
- Record old formatting in `w:rPrChange` or `w:pPrChange`. When splitting a run with an earlier
  formatting revision, retain its author, date, old properties and text coverage. Give copies unique IDs.
- A comment needs its entry, range anchors/reference, relationship and content type.

## Optional diagnostics and preview

`check_docx(task_id=...)` compares the snapshot and candidate. In path mode, absolute paths
can select two files outside the private workspace. For files inside it, the tool expects
`original` in `inputs/` and `candidate` in `work/`. Confirm the pair in `comparison`.

Diagnostics assume additive revision history. Requested acceptance, rejection or comment
removal can change that baseline. Interpret observations against the requested work and
investigate concrete unexpected loss. Revisions with overlapping metadata can be hard to
attribute. `changes` gives paragraph excerpts, including direct strikethrough. Follow
`next_start` with `changes_start` for more. Use `read_docx(path, format="data")` for full
comment records and anchor markup, which can include retained deleted text.

Preview page numbers belong to the LibreOffice render and can shift when content moves.
Cite clause numbers or text as locators. Preview phrase search uses stored page text, with no
OCR or cross-page matches. Re-render after edits. At nested deletions, LibreOffice may omit
strikethrough. Use `preview_guidance` to locate affected passages and `read_docx` for full
projected text. Review complex history in Word when needed. Report structural checks,
images actually reviewed, native Word opening and Word accept/reject as distinct checks.

## Saving and interrupted calls

`from veqtor_workspace.files import atomic_save` provides a safe candidate write.
Use `with atomic_save(candidate_path) as draft:` and write, close and validate `draft` inside
the block. Exiting successfully replaces the candidate. Use `overwrite=False` for a new
file at an unused path. A completed replacement survives a later script error. If a Python
call is interrupted, inspect the candidate before repeating any edit.

`document_task(save)` publishes the candidate alone. Checkpoints and other work files
stay in the task. In-place save checks the source against the snapshot and a new destination
must be unused in an existing folder. Read the saved file using the returned `read_hint`.
Start a fresh task from the latest saved version for later edits.

After an interrupted save, use `document_task(status)` for the recorded destination.
Possible results are `saved`, `not_saved`, `saved_then_changed` and `outcome_unknown`.
A recorded save intent closes editing even if the final status write fails. Resolve any
uncertain outcome before retrying a save.
