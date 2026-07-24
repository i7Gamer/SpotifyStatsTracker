/* The admin console "Create backup now" button form: submitted via fetch so
 * triggering a snapshot doesn't reload the page or navigate away - the route
 * answers XHR posts with {kind, message} JSON (see adminCreateBackup), rendered
 * into the #backup-status-message slot inside the Backups card. */
(function () {
  var FLASH_COLORS = { error: 'var(--danger, #e05252)', success: 'var(--accent, #1db954)' };

  function getBackupFlashColor(kind) {
    return FLASH_COLORS[kind] || FLASH_COLORS.error;
  }

  function formatBackupStatusPayload(data) {
    if (!data || typeof data !== 'object') {
      return { kind: 'error', message: 'Backup failed — invalid server response.' };
    }
    var kind = data.kind === 'success' ? 'success' : 'error';
    var message = typeof data.message === 'string' && data.message.length > 0
      ? data.message
      : (kind === 'success' ? 'Database snapshot created successfully.' : 'Backup failed — try again.');
    return { kind: kind, message: message };
  }

  function showMessage(kind, message) {
    var container = document.getElementById('backup-status-message');
    if (!container) {
      return;
    }
    container.innerHTML = '';
    var card = document.createElement('div');
    card.style.border = '1px solid ' + getBackupFlashColor(kind);
    card.style.borderRadius = '4px';
    card.style.padding = '0.5rem 1rem';
    card.style.marginBottom = '0.75rem';
    var p = document.createElement('p');
    p.style.margin = '0';
    p.style.color = getBackupFlashColor(kind);
    p.style.fontSize = '0.85rem';
    p.textContent = message;
    card.appendChild(p);
    container.appendChild(card);
  }

  function initAdminBackupForm() {
    var form = document.querySelector('form[action*="/admin/create_backup"]');
    if (!form) {
      return;
    }

    form.addEventListener('submit', function (evt) {
      evt.preventDefault();
      var button = form.querySelector('button[type="submit"]');
      var originalText = button ? button.textContent : '';
      if (button) {
        button.disabled = true;
        button.textContent = 'Creating backup…';
      }
      fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        headers: { 'X-Requested-With': 'XMLHttpRequest' }
      })
        .then(function (resp) { return resp.json(); })
        .then(function (data) {
          var payload = formatBackupStatusPayload(data);
          showMessage(payload.kind, payload.message);
        })
        .catch(function () { showMessage('error', 'Backup failed — try again.'); })
        .finally(function () {
          if (button) {
            button.disabled = false;
            button.textContent = originalText;
          }
        });
    });
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initAdminBackupForm);
    } else {
      initAdminBackupForm();
    }
  }

  var exportsObj = {
    getBackupFlashColor: getBackupFlashColor,
    formatBackupStatusPayload: formatBackupStatusPayload,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = exportsObj;
  }
})();
