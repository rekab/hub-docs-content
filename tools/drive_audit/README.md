# tools/drive_audit — Apps Script that produces the audit CSV

`audit.gs` is the upstream Google Apps Script that walks a Drive folder
and writes a sharing-state row for every item to a Google Sheet. The
Sheet is what you download as CSV and drop into
`../inputs/william-hub-audit-*.csv` for the Python pipeline to consume.

This script is read-only. It never mutates Drive — it inspects sharing
settings and records them.

## Setup (one-time)

1. **Create the Apps Script project.** Go to
   https://script.google.com/, **New project**. Name it something like
   "Drive Sharing Audit."

2. **Paste in `audit.gs`.** Replace the default `Code.gs` contents with
   the contents of this file.

3. **Set the root folder ID.** In the Apps Script editor:
   **Project Settings → Script Properties → Add script property**.
   - Property: `root_folder_id`
   - Value: the long ID from the Drive folder URL (the part after
     `https://drive.google.com/drive/folders/`).

4. **Grant Drive access on first run.** The first call to
   `auditSharing()` will prompt you to authorize the script. It needs
   the Drive scope to read folder + file metadata.

## Running

In the Apps Script editor, select `auditSharing` from the function
dropdown and click **Run**.

A consumer Google account's Apps Script execution is capped at 6 min
of wall-clock per invocation. This script stops cleanly at 5.5 min and
persists its progress; just **Run** again to continue. Repeat until the
log says "Audit complete."

After completion, open the Sheet (URL is logged on the last run). Use
**File → Download → Comma-separated values (.csv)** to export it, then
drop the CSV into `../inputs/` for the Python pipeline.

## State and reset

The script stores three things in Script Properties:

| Key | What it holds |
|---|---|
| `root_folder_id` | the Drive folder to audit (you set this) |
| `audit_sheet_id` | the Sheet currently being appended to |
| `completed_folder_ids` | folders whose self + files + all subtrees are done |
| `partial_folder_state` | folders whose self + direct files are recorded but whose subfolders aren't yet done — prevents duplicate rows on resume |

To start over with a fresh Sheet:

```
resetAudit()
```

That clears the sheet pointer and the two progress sets but
**preserves** `root_folder_id` — you don't need to re-enter it. To
change which folder is audited, edit the `root_folder_id` Script
Property directly.

## What gets written

Every row in the Sheet has these columns:

| Column | Notes |
|---|---|
| Path | folder hierarchy with ` / ` separator, e.g. `Hub Captain Documents / Photos / 2025` |
| Type | `folder` or `file` |
| Name | Drive's display name |
| Status | `OK` (matches target sharing), `NOT_LINK_SHARED` (more restrictive than target), `OVER_PERMISSIONED` (anyone-with-link with EDIT instead of VIEW), or `ERROR` |
| Access | raw `DriveApp.Access` value (e.g. `ANYONE_WITH_LINK`, `PRIVATE`) |
| Permission | raw `DriveApp.Permission` value (e.g. `VIEW`, `EDIT`) |
| Owner | owner's email, or `(no individual owner)` for shared drives |
| URL | direct Drive URL |

Target sharing (the definition of "OK") is encoded at the top of the
script: `ANYONE_WITH_LINK` + `VIEW`. Edit `TARGET_ACCESS` and
`TARGET_PERMISSION` if your definition of "correct" differs.

## Performance note

The current implementation uses `DriveApp.Folder.getFiles()` /
`getFolders()`, which makes one RPC per child item. For a ~3,000-item
audit, expect roughly 4–6 invocations of `auditSharing()` to finish.

A real perf upgrade would switch to the advanced Drive REST API
(`Drive.Files.list` with `fields` projection), which can pull ~1,000
items + permissions per call. That'd cut the wall-clock by an
order of magnitude. Not done here because the resumability handles
multi-invocation cleanly and we don't expect to re-run this often.
