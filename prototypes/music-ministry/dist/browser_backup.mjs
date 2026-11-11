// Portable backup of the V19 preview's browser-owned state only.
// Never include arbitrary localStorage keys, external sessions, or private server snapshots.
export const BACKUP_FORMAT = 'wcfi-music-local-backup';
export const BACKUP_VERSION = 1;
export const BACKUP_MAX_RECORDS = 600;
export const BACKUP_MAX_BYTES = 8 * 1024 * 1024;

const EXACT_KEYS = new Set([
  'wcfi-music-preview-v1',  // legacy personal drafts and preferences
  'wcfi-music-hub-v2',      // current personal lineups, draft and preferences
  'wcfi-song-workshop-v1',  // songs, deletions and service-specific arrangements
  'wcfi-song-editor-last',  // unfinished editor's resume pointer
]);
const DRAFT_PREFIX = 'wcfi-song-editor-draft-v1:';
const DRAFT_ID = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/;

export function isMusicBackupKey(key) {
  return typeof key === 'string' && (
    EXACT_KEYS.has(key) ||
    (key.startsWith(DRAFT_PREFIX) && DRAFT_ID.test(key.slice(DRAFT_PREFIX.length)))
  );
}

function utf8Size(value) {
  return new TextEncoder().encode(value).byteLength;
}

export function validateLocalBackup(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      value.format !== BACKUP_FORMAT || value.version !== BACKUP_VERSION ||
      value.scope !== 'browser-local-only' ||
      typeof value.exportedAt !== 'string' || !Number.isFinite(Date.parse(value.exportedAt)) ||
      !Array.isArray(value.records) || value.records.length > BACKUP_MAX_RECORDS) {
    throw new Error('Invalid WCFI local backup envelope.');
  }
  const seen = new Set();
  for (const row of value.records) {
    if (!row || typeof row !== 'object' || Array.isArray(row) ||
        !isMusicBackupKey(row.key) || typeof row.value !== 'string' || seen.has(row.key)) {
      throw new Error('Invalid, duplicate or non-WCFI storage key in backup.');
    }
    seen.add(row.key);
  }
  if (utf8Size(JSON.stringify(value)) > BACKUP_MAX_BYTES) {
    throw new Error('Local backup exceeds the size limit.');
  }
  return value;
}

// Read-only capture. Preserve raw strings exactly, including malformed/incomplete draft JSON.
// A download is only started by the separate, user-initiated UI handler.
export function captureLocalBackup(storage, exportedAt = new Date().toISOString()) {
  if (!storage || typeof storage.length !== 'number' ||
      typeof storage.key !== 'function' || typeof storage.getItem !== 'function') {
    throw new Error('Browser storage is unavailable.');
  }
  const keys = [];
  for (let i = 0; i < storage.length; i++) {
    const key = storage.key(i);
    if (isMusicBackupKey(key)) keys.push(key);
  }
  if (keys.length > BACKUP_MAX_RECORDS) throw new Error('Too many local song drafts to back up.');
  const records = keys.sort().map(key => {
    const value = storage.getItem(key);
    if (typeof value !== 'string') throw new Error(`Browser storage changed during backup: ${key}`);
    return {key, value};
  });
  return validateLocalBackup({
    format: BACKUP_FORMAT, version: BACKUP_VERSION,
    exportedAt, scope: 'browser-local-only', records,
  });
}

// For future migration analysis. It does not restore, change or clear browser storage.
export function inspectLocalBackup(value) {
  const backup = validateLocalBackup(value);
  const keys = backup.records.map(row => row.key);
  return {
    recordCount: keys.length,
    editorDraftCount: keys.filter(key => key.startsWith(DRAFT_PREFIX)).length,
    hasSongWorkshop: keys.includes('wcfi-song-workshop-v1'),
    hasLineups: keys.includes('wcfi-music-hub-v2'),
    includesLegacy: keys.includes('wcfi-music-preview-v1'),
    hasEditorResumePointer: keys.includes('wcfi-song-editor-last'),
  };
}