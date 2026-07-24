// Plain-node unit test for the admin backup JS logic (static/js/admin-backup.js).
// Run with: node tests/test_admin_backup.js
const assert = require('assert');
const { getBackupFlashColor, formatBackupStatusPayload } = require('../static/js/admin-backup.js');

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

run('returns success color for success kind', () => {
  assert.strictEqual(getBackupFlashColor('success'), 'var(--accent, #1db954)');
});

run('returns danger color for error or unknown kind', () => {
  assert.strictEqual(getBackupFlashColor('error'), 'var(--danger, #e05252)');
  assert.strictEqual(getBackupFlashColor('unknown'), 'var(--danger, #e05252)');
});

run('formats valid success payload correctly', () => {
  const res = formatBackupStatusPayload({ kind: 'success', message: 'Snapshot created: backup_1.db' });
  assert.deepStrictEqual(res, {
    kind: 'success',
    message: 'Snapshot created: backup_1.db'
  });
});

run('formats error payload correctly', () => {
  const res = formatBackupStatusPayload({ kind: 'error', message: 'Disk full' });
  assert.deepStrictEqual(res, {
    kind: 'error',
    message: 'Disk full'
  });
});

run('handles invalid or null payload gracefully', () => {
  const nullRes = formatBackupStatusPayload(null);
  assert.strictEqual(nullRes.kind, 'error');
  assert.strictEqual(nullRes.message, 'Backup failed — invalid server response.');

  const emptyMsgRes = formatBackupStatusPayload({ kind: 'success', message: '' });
  assert.strictEqual(emptyMsgRes.kind, 'success');
  assert.strictEqual(emptyMsgRes.message, 'Database snapshot created successfully.');
});

console.log('All admin-backup JS tests passed.');
