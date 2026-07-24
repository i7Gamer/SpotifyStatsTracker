/* Shared failure UI for the AJAX shell pages (history, charts, genres, compare,
 * dashboard filter). Before this, a failed initial/filter fetch left a page
 * stuck on "Loading…" or a blank canvas with only a console.error - the user
 * had no signal and no way to retry. Two presentations:
 *   renderInto(target, onRetry) - replace a single swap target's contents with
 *     an inline "couldn't load - Retry" block (history, dashboard cards).
 *   showBanner(onRetry) / clearBanner() - a page-level banner for pages whose
 *     swap targets are canvases or many small regions (charts, genres, compare).
 * onRetry is the page's own loader; Retry clears the error and re-fires it.
 * Exposed on window; also module.exports so the API contract can be unit-tested
 * under node. */
(function () {
  var BANNER_ID = 'ajax-error-banner';
  var DEFAULT_MESSAGE = "Couldn't load the latest data.";

  function buildRetryButton(onRetry) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ajax-error-retry';
    btn.textContent = 'Retry';
    btn.addEventListener('click', function () { onRetry(); });
    return btn;
  }

  function renderInto(target, onRetry, message) {
    if (!target) return;
    target.classList.remove('loading-fade');
    target.innerHTML = '';
    var wrap = document.createElement('div');
    wrap.className = 'ajax-error-inline';
    var text = document.createElement('p');
    text.textContent = message || DEFAULT_MESSAGE;
    wrap.appendChild(text);
    wrap.appendChild(buildRetryButton(onRetry));
    target.appendChild(wrap);
  }

  function showBanner(onRetry, message) {
    var host = document.querySelector('main') || document.body;
    if (!host) return;
    var banner = document.getElementById(BANNER_ID);
    if (!banner) {
      banner = document.createElement('div');
      banner.id = BANNER_ID;
      banner.className = 'ajax-error-banner';
      host.insertBefore(banner, host.firstChild);
    }
    banner.innerHTML = '';
    var text = document.createElement('span');
    text.textContent = message || DEFAULT_MESSAGE;
    banner.appendChild(text);
    banner.appendChild(buildRetryButton(function () {
      clearBanner();
      onRetry();
    }));
  }

  function clearBanner() {
    var banner = document.getElementById(BANNER_ID);
    if (banner && banner.parentNode) {
      banner.parentNode.removeChild(banner);
    }
  }

  var AjaxStatus = {
    renderInto: renderInto,
    showBanner: showBanner,
    clearBanner: clearBanner,
    DEFAULT_MESSAGE: DEFAULT_MESSAGE,
  };

  if (typeof window !== 'undefined') {
    window.AjaxStatus = AjaxStatus;
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = AjaxStatus;
  }
})();
