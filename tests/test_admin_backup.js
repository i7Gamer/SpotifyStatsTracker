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

// ---- The submit path -------------------------------------------------------
//
// An expired session used to arrive as a 302 that fetch followed to the login
// page, so resp.json() threw and the admin was told "Backup failed - try
// again". The backup had not failed; they were logged out, and every retry
// that message invites fails the same way. The guard now answers 401 (see
// unauthenticatedResponse) and this file has to act on it.
const FORM_SELECTOR = 'form[action*="/admin/create_backup"]';

function driveSubmit(respond) {
  const button = { disabled: false, textContent: 'Create backup now' };
  const handlers = {};
  const container = { innerHTML: null, children: [], appendChild(node) { this.children.push(node); } };
  const form = {
    action: '/admin/create_backup',
    addEventListener(type, fn) { handlers[type] = fn; },
    querySelector(sel) { return sel === 'button[type="submit"]' ? button : null; },
  };
  const redirected = [];

  global.window.AjaxStatus = {
    readJsonOrThrow(resp) {
      if (resp && resp.status === 401) {
        redirected.push(resp);
        throw new Error('ajax-unauthorized');
      }
      if (resp && resp.ok === false) {
        throw new Error('backup fetch failed: ' + resp.status);
      }
      return resp.json();
    },
    isUnauthorizedError(err) { return !!err && err.message === 'ajax-unauthorized'; },
  };
  global.FormData = function FormDataStub(f) { this.form = f; };
  global.document = {
    querySelector(sel) { return sel === FORM_SELECTOR ? form : null; },
    getElementById(id) { return id === 'backup-status-message' ? container : null; },
    createElement(tag) { return { tag, style: {}, textContent: '', appendChild() {} }; },
  };
  global.fetch = function () { return respond(); };

  global.window.initAdminBackupForm();
  handlers.submit({ preventDefault() {} });

  return { button, container, redirected };
}

async function settle() {
  //< the chain is .then().then().catch().finally()
  for (let i = 0; i < 3; i += 1) {
    await new Promise(resolve => setImmediate(resolve));
  }
}

(async () => {
  {
    const { container } = driveSubmit(() => Promise.resolve({
      ok: false, status: 401, json: () => Promise.resolve({ error: 'Not logged in' }),
    }));
    await settle();
    run('a logged-out admin is sent to log in rather than told the backup failed', () => {
      assert.strictEqual(container.children.length, 0,
        'AjaxStatus is already navigating; a failure card would blame the wrong thing');
    });
  }

  {
    const { redirected } = driveSubmit(() => Promise.resolve({
      ok: false, status: 401, json: () => Promise.resolve({}),
    }));
    await settle();
    run('the 401 reaches AjaxStatus, which is what performs the navigation', () => {
      assert.strictEqual(redirected.length, 1);
    });
  }

  {
    const { button } = driveSubmit(() => Promise.resolve({
      ok: false, status: 401, json: () => Promise.resolve({}),
    }));
    await settle();
    run('the button and its label come back after a 401', () => {
      assert.strictEqual(button.disabled, false, 'the finally must run on every path');
      assert.strictEqual(button.textContent, 'Create backup now');
    });
  }

  {
    const { container } = driveSubmit(() => Promise.resolve({
      ok: false, status: 500, json: () => Promise.resolve({}),
    }));
    await settle();
    run('a real server error still reports the backup as failed', () => {
      assert.strictEqual(container.children.length, 1);
    });
  }

  {
    const { container } = driveSubmit(() => Promise.resolve({
      ok: true, status: 200, json: () => Promise.resolve({ kind: 'success', message: 'Snapshot created' }),
    }));
    await settle();
    run('a successful backup still renders its message', () => {
      assert.strictEqual(container.children.length, 1);
    });
  }

  console.log('All admin-backup JS tests passed.');
})();
