// Plain-node unit test for the admin backup JS logic (static/js/admin-backup.js).
// Run with: node tests/test_admin_backup.js
const assert = require('assert');
// Before the require: the file publishes its initialiser onto window at load
// time, guarded on `typeof window !== 'undefined'`, and plain node has no
// window. Without this stub the guard is simply false and the hook admin-page.js
// depends on would look absent whether or not it is there.
global.window = global.window || {};
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

// The Backups card lives inside #admin-tab-body, so an AJAX subnav switch to
// the Settings tab replaces the form this file bound on load. admin-page.js
// re-initialises the swapped-in body by calling window.initAdminBackupForm();
// the function existed only inside this file's IIFE, so that hook was dead and
// "Create backup now" fell back to a full-page POST - the same button behaving
// two ways depending on how you reached the tab. Pinned as the contract
// admin-page.js actually depends on, by name.
run('exposes initAdminBackupForm for admin-page.js to re-run after a tab swap', () => {
  const exported = require('../static/js/admin-backup.js');
  assert.strictEqual(typeof exported.initAdminBackupForm, 'function',
    'admin-backup.js must export initAdminBackupForm');
  assert.strictEqual(typeof global.window.initAdminBackupForm, 'function',
    'admin-page.js looks it up on window, so window is where it must land');
});

console.log('All admin-backup JS tests passed.');
