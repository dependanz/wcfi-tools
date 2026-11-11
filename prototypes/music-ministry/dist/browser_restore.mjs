// Restore support for explicitly downloaded V19 browser-local backups.
// This module never fetches, uploads, clears or overwrites existing Music Ministry data.
import {BACKUP_MAX_BYTES, inspectLocalBackup, isMusicBackupKey, validateLocalBackup} from './browser_backup.mjs';

function checkStorage(storage) {
  if (!storage || typeof storage.length !== 'number' ||
      typeof storage.key !== 'function' || typeof storage.getItem !== 'function') {
    throw new Error('Browser storage is unavailable.');
  }
}

function existingMusicKeys(storage) {
  checkStorage(storage);
  const keys = [];
  for (let index = 0; index < storage.length; index++) {
    const key = storage.key(index);
    if (isMusicBackupKey(key)) keys.push(key);
  }
  return keys.sort();
}

// Dry-run: strictly read-only. A different origin/port has separate browser storage.
export function planLocalRestore(storage, envelope) {
  const backup = validateLocalBackup(envelope);
  if (!backup.records.length) throw new Error('This backup contains no local records to restore.');
  const existingKeys = existingMusicKeys(storage);
  return {
    ...inspectLocalBackup(backup),
    backupDate: backup.exportedAt,
    existingKeys,
    safeToRestore: existingKeys.length === 0,
    sourceBytes: new TextEncoder().encode(JSON.stringify(backup)).byteLength,
    maxBytes: BACKUP_MAX_BYTES,
  };
}

// Does not accept an overwrite option. Explicit caller confirmation belongs in the UI.
// All records are validated before the first write. If any write fails, revert everything
// added by this attempt, and report inability to roll back rather than claiming success.
export function restoreLocalBackupIntoEmptyStorage(storage, envelope) {
  const plan = planLocalRestore(storage, envelope);
  if (!plan.safeToRestore) {
    throw new Error('Destination already contains Music Ministry data. Nothing was changed.');
  }
  if (typeof storage.setItem !== 'function' || typeof storage.removeItem !== 'function') {
    throw new Error('Browser storage is not writable.');
  }
  const written = [];
  try {
    for (const row of envelope.records) {
      if (storage.getItem(row.key) !== null) {
        throw new Error('Destination changed during restore.');
      }
      // Register intent before writing, since a storage adapter may write then throw.
      written.push(row.key);
      storage.setItem(row.key, row.value);
      // Treat a write that reports success but does not persist as a failure.
      if (storage.getItem(row.key) !== row.value) {
        throw new Error('Browser storage did not retain a restored record.');
      }
    }
    return {restoredRecords: written.length};
  } catch (error) {
    const failedRollbacks = [];
    for (const key of [...written].reverse()) {
      try {
        storage.removeItem(key);
        if (storage.getItem(key) !== null) failedRollbacks.push(key);
      } catch {
        failedRollbacks.push(key);
      }
    }
    if (failedRollbacks.length) {
      throw new Error('Restore failed; automatic rollback was incomplete. Preserve this browser and the backup file.');
    }
    throw new Error('Restore failed and was rolled back; no saved Music Ministry values were intentionally replaced.', {cause:error});
  }
}
