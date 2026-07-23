/* The detail pages' admin "Refresh Last.fm Data" form: submitted via fetch so
 * a refresh doesn't navigate away and reset the tab/sort/page state - the
 * route answers XHR posts with {kind, message} JSON instead of redirecting
 * (see adminRefreshLastfmEntity), shown in the #detail-flash slot the
 * redirect fallback also renders into. No-ops for non-admins (no form). */
(function () {
  var form = document.querySelector('form[action*="/admin/lastfm/refresh/"]');
  if (!form) {
    return;
  }

  var FLASH_COLORS = { error: '#ff4a4a', success: '#4aff4a' };

  function showFlash(kind, message) {
    var flash = document.getElementById('detail-flash');
    if (!flash) {
      return;
    }
    flash.innerHTML = '';
    var p = document.createElement('p');
    p.className = kind;
    p.style.color = FLASH_COLORS[kind] || FLASH_COLORS.error;
    p.style.marginBottom = '1rem';
    p.textContent = message;
    flash.appendChild(p);
  }

  form.addEventListener('submit', function (evt) {
    evt.preventDefault();
    var button = form.querySelector('button[type="submit"]');
    if (button) {
      button.disabled = true;
    }
    fetch(form.action, {
      method: 'POST',
      body: new FormData(form),
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    })
      .then(function (resp) { return resp.json(); })
      .then(function (data) { showFlash(data.kind, data.message); })
      .catch(function () { showFlash('error', 'Refresh failed - try again.'); })
      .finally(function () {
        if (button) {
          button.disabled = false;
        }
      });
  });
})();
