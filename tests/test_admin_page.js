// Plain-node unit test for admin page AJAX tab logic (static/js/admin-page.js).
// Run with: node tests/test_admin_page.js
const assert = require('assert');

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

// Mock DOM environment for node execution
function setupMockWindow(initialUrl = 'http://localhost/admin') {
  const listeners = {};
  let currentState = null;
  let currentUrl = initialUrl;

  const document = {
    querySelector: (sel) => {
      if (sel === '.admin-subnav') {
        return {
          _adminAjaxWired: false,
          addEventListener: (evt, handler) => {
            listeners[evt] = handler;
          },
          querySelectorAll: () => []
        };
      }
      return null;
    },
    getElementById: (id) => {
      if (id === 'admin-tab-body') {
        return {
          innerHTML: '<div>Initial Content</div>',
          classList: {
            add: () => {},
            remove: () => {}
          },
          querySelectorAll: () => []
        };
      }
      return null;
    }
  };

  const history = {
    state: null,
    replaceState: (state, title, url) => {
      currentState = state;
      if (url) currentUrl = url;
      history.state = state;
    },
    pushState: (state, title, url) => {
      currentState = state;
      if (url) currentUrl = url;
      history.state = state;
    }
  };

  const windowMock = {
    location: {
      href: currentUrl,
      origin: 'http://localhost'
    },
    history,
    document,
    addEventListener: (evt, handler) => {
      listeners[evt] = handler;
    },
    listeners,
    getCurrentState: () => currentState
  };

  return windowMock;
}

run('init sets initial history state if null', () => {
  const win = setupMockWindow('http://localhost/admin?tab=overview');
  global.window = win;
  global.document = win.document;
  global.history = win.history;
  global.location = win.location;

  // Clear module cache to re-require with fresh window mock
  delete require.cache[require.resolve('../static/js/admin-page.js')];
  const AdminPage = require('../static/js/admin-page.js');

  AdminPage.init();

  assert.notStrictEqual(win.history.state, null, 'history.state should not be null after init');
  assert.strictEqual(win.history.state.adminTab, 'http://localhost/admin?tab=overview');
});

console.log('All admin-page JS tests passed.');
