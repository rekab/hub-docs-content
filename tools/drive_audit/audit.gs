/**
 * AUDIT-ONLY (resumable): walks a Drive folder and reports the
 * sharing state of every item visible to you. Read-only.
 *
 * Setup (one-time):
 *   In the Apps Script editor: Project Settings → Script Properties →
 *   Add property. Set:
 *     root_folder_id = <the Drive folder ID to audit>
 *   The folder ID is the long opaque string in the URL when you open
 *   the folder in Drive: https://drive.google.com/drive/folders/<ID>
 *
 * Resumability: tracks per-folder progress in PropertiesService and
 * appends to the same Google Sheet across runs. Apps Script consumer
 * accounts have a 6-min wall-clock limit per execution; this script
 * stops at 5.5 min and resumes cleanly on the next invocation. Re-run
 * auditSharing() until it logs "Audit complete."
 *
 * Functions:
 *   auditSharing()  — start or continue the audit
 *   resetAudit()    — clear state so the NEXT auditSharing() starts fresh
 */

const TARGET_ACCESS = DriveApp.Access.ANYONE_WITH_LINK;
const TARGET_PERMISSION = DriveApp.Permission.VIEW;

const PROPS = PropertiesService.getScriptProperties();
const ROOT_FOLDER_ID_KEY = 'root_folder_id';
const COMPLETED_KEY = 'completed_folder_ids';
// folderId -> "filesDone" once its self + direct files have been recorded,
// even if some subfolders are still pending. Avoids re-recording on resume.
const PARTIAL_KEY = 'partial_folder_state';
const SHEET_KEY = 'audit_sheet_id';

function getRootFolderId_() {
  const id = PROPS.getProperty(ROOT_FOLDER_ID_KEY);
  if (!id) {
    throw new Error(
      'Missing Script Property "' + ROOT_FOLDER_ID_KEY + '". ' +
      'In the Apps Script editor, go to Project Settings → Script Properties ' +
      'and add it (the long ID from the Drive folder URL).'
    );
  }
  return id;
}

function auditSharing() {
  const rootId = getRootFolderId_();
  const ss = getOrCreateSheet_();
  const sheet = ss.getActiveSheet();
  const completed = loadCompleted_();
  const filesDone = loadFilesDone_();
  console.log(`Resuming. ${completed.size} folders fully done; ` +
              `${filesDone.size} folders had their direct files recorded.`);

  const buffer = [];
  const stats = {
    total: 0, ok: 0, issues: 0, errors: 0,
    skipped_folders: 0, skipped_files: 0,
  };
  const start = Date.now();
  const root = DriveApp.getFolderById(rootId);

  try {
    walk_(root, root.getName(), buffer, stats, start, sheet, completed, filesDone);
    flush_(buffer, sheet);
    saveCompleted_(completed);
    saveFilesDone_(filesDone);
    console.log('Audit complete. Sheet: ' + ss.getUrl());
    console.log('To start over, run resetAudit() then auditSharing().');
  } catch (e) {
    flush_(buffer, sheet);
    saveCompleted_(completed);
    saveFilesDone_(filesDone);
    if (e.message === 'TIME_LIMIT') {
      console.log(`Time limit hit. ${completed.size} folders done so far.`);
      console.log('Run auditSharing() again to continue. Sheet: ' + ss.getUrl());
    } else {
      throw e;
    }
  }
  console.log('Stats this run: ' + JSON.stringify(stats));
}

function resetAudit() {
  PROPS.deleteProperty(COMPLETED_KEY);
  PROPS.deleteProperty(PARTIAL_KEY);
  PROPS.deleteProperty(SHEET_KEY);
  console.log('State cleared. Next auditSharing() will start fresh in a new sheet.');
  console.log('Note: root_folder_id is preserved; reset it separately if needed.');
}

function walk_(folder, path, buffer, stats, start, sheet, completed, filesDone) {
  if (Date.now() - start > 5.5 * 60 * 1000) throw new Error('TIME_LIMIT');

  const folderId = folder.getId();
  if (completed.has(folderId)) {
    stats.skipped_folders++;
    return;
  }

  // Record self + direct files exactly once, even across resumes. A
  // previous run may have completed this stage but timed out walking
  // subfolders; in that case filesDone.has(folderId) is true and we
  // skip straight to subfolder recursion.
  if (!filesDone.has(folderId)) {
    record_(folder, path, 'folder', buffer, stats);
    if (buffer.length >= 100) flush_(buffer, sheet);

    const files = folder.getFiles();
    while (files.hasNext()) {
      record_(files.next(), path, 'file', buffer, stats);
      if (buffer.length >= 100) flush_(buffer, sheet);
    }
    filesDone.add(folderId);
    // Checkpoint partial state so a timeout mid-walk doesn't lose this.
    if (filesDone.size % 25 === 0) saveFilesDone_(filesDone);
  } else {
    stats.skipped_files++;
  }

  const subfolders = folder.getFolders();
  while (subfolders.hasNext()) {
    const sub = subfolders.next();
    walk_(sub, path + ' / ' + sub.getName(),
          buffer, stats, start, sheet, completed, filesDone);
  }

  // Only mark complete once ALL descendants are done.
  completed.add(folderId);
  if (completed.size % 25 === 0) saveCompleted_(completed);
}

function record_(item, path, kind, buffer, stats) {
  stats.total++;
  try {
    const access = item.getSharingAccess();
    const perm = item.getSharingPermission();
    const owner = item.getOwner();
    const ownerEmail = owner ? owner.getEmail() : '(no individual owner)';

    let status;
    if (access === TARGET_ACCESS && perm === TARGET_PERMISSION) {
      status = 'OK';
      stats.ok++;
    } else if (access !== TARGET_ACCESS) {
      status = 'NOT_LINK_SHARED';
      stats.issues++;
    } else {
      status = 'OVER_PERMISSIONED';
      stats.issues++;
    }

    buffer.push([
      path, kind, item.getName(), status,
      String(access), String(perm), ownerEmail, item.getUrl()
    ]);
  } catch (e) {
    stats.errors++;
    buffer.push([path, kind, item.getName(), 'ERROR', '', '', '', e.message]);
  }
}

function flush_(buffer, sheet) {
  if (buffer.length === 0) return;
  sheet.getRange(sheet.getLastRow() + 1, 1, buffer.length, buffer[0].length)
       .setValues(buffer);
  buffer.length = 0;
}

function loadCompleted_() {
  const raw = PROPS.getProperty(COMPLETED_KEY);
  return new Set(raw ? JSON.parse(raw) : []);
}

function saveCompleted_(set) {
  PROPS.setProperty(COMPLETED_KEY, JSON.stringify([...set]));
}

function loadFilesDone_() {
  const raw = PROPS.getProperty(PARTIAL_KEY);
  return new Set(raw ? JSON.parse(raw) : []);
}

function saveFilesDone_(set) {
  PROPS.setProperty(PARTIAL_KEY, JSON.stringify([...set]));
}

function getOrCreateSheet_() {
  const id = PROPS.getProperty(SHEET_KEY);
  if (id) {
    try { return SpreadsheetApp.openById(id); }
    catch (e) { /* sheet was deleted; fall through and make a new one */ }
  }
  const ss = SpreadsheetApp.create(
    'Drive Sharing Audit — ' + new Date().toISOString().slice(0, 19)
  );
  const sheet = ss.getActiveSheet();
  sheet.appendRow([
    'Path', 'Type', 'Name', 'Status', 'Access', 'Permission', 'Owner', 'URL'
  ]);
  sheet.setFrozenRows(1);
  PROPS.setProperty(SHEET_KEY, ss.getId());
  return ss;
}
