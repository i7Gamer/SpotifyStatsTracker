// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

/* Shared failure UI for the AJAX shell pages (history, charts, genres, compare,
 * dashboard filter). Before this, a failed initial/filter fetch left a page
 * stuck on "Loading…" or a blank canvas with only a console.error - the user
 * had no signal and no way to retry. Two presentations:
 *   renderInto(target, onRetry) - replace a single swap target's contents with
 *     an inline "couldn't load - Retry" block (history, dashboard cards).
 *   showBanner(onRetry, message, owner) / clearBanner(owner) - a page-level
 *     banner for pages whose swap targets are canvases or many small regions
 *     (charts, genres, compare). `owner` is for a page with TWO loaders sharing
 *     the slot (the detail pages' chart and play log): a clear that names an
 *     owner only removes that owner's banner, so one loader's success cannot
 *     take down the other's failure. A single-owner page passes none.
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

  //< who last put the banner up (see the header). The slot is reused rather
  //  than stacked, so the loader that reported most recently owns it.
  var bannerOwner;

  function showBanner(onRetry, message, owner) {
    var host = document.querySelector('main') || document.body;
    if (!host) return;
    var banner = document.getElementById(BANNER_ID);
    if (!banner) {
      banner = document.createElement('div');
      banner.id = BANNER_ID;
      banner.className = 'ajax-error-banner';
      host.insertBefore(banner, host.firstChild);
    }
    bannerOwner = owner;
    banner.innerHTML = '';
    var text = document.createElement('span');
    text.textContent = message || DEFAULT_MESSAGE;
    banner.appendChild(text);
    banner.appendChild(buildRetryButton(function () {
      clearBanner();
      onRetry();
    }));
  }

  // A clear that names an owner is "MY request succeeded" - no news about a
  // banner someone else (or nobody) put up, so it leaves that one alone. A
  // clear naming none is the Retry button's own, and the single-owner pages'.
  function clearBanner(owner) {
    if (owner !== undefined && owner !== bannerOwner) return;
    var banner = document.getElementById(BANNER_ID);
    if (banner && banner.parentNode) {
      banner.parentNode.removeChild(banner);
    }
    bannerOwner = undefined;
  }

  // An expired session used to leave these pages dead-ended: the route sent a
  // 302 to /login, fetch followed it transparently, and the page tried to parse
  // the login HTML as JSON - so the user got "couldn't load / Retry", and Retry
  // failed the same way forever. The routes now answer an ajax request with a
  // 401, and every loader routes it here to send the user to log in (coming
  // back to the page they were on).
  function redirectIfUnauthorized(resp) {
    if (!resp || resp.status !== 401) return false;
    var next = window.location.pathname + window.location.search;
    window.location.href = '/login?next=' + encodeURIComponent(next);
    return true;
  }

  // Thrown by a loader after redirectIfUnauthorized() has started navigating,
  // so its catch can tell "we're leaving the page" apart from a real failure
  // and skip the error banner.
  var UNAUTHORIZED_ERROR = 'ajax-unauthorized';

  function isUnauthorizedError(err) {
    return !!err && err.message === UNAUTHORIZED_ERROR;
  }

  // The two checks every AJAX loader must make before touching the payload,
  // written once instead of nine times.
  //
  // Both were separately forgotten in production. A 401 answers with a JSON body,
  // so `.json()` RESOLVES and the payload key is simply absent - the dashboard and
  // /history each rendered the string "undefined" over their content. And a
  // non-2xx handed to `resp.ok ? resp.json() : null` reads to the caller exactly
  // like "nothing to do", which left placeholders loading forever and stale lists
  // under URLs that said otherwise.
  //
  // Returning the parsed JSON (rather than the response) is deliberate: it leaves
  // a caller no way to reach the body without passing through here.
  function readJsonOrThrow(resp, label) {
    if (redirectIfUnauthorized(resp)) {
      throw new Error(UNAUTHORIZED_ERROR);
    }
    if (!resp.ok) {
      throw new Error((label || 'ajax') + ' fetch failed: ' + resp.status);
    }
    return resp.json();
  }

  var AjaxStatus = {
    renderInto: renderInto,
    showBanner: showBanner,
    clearBanner: clearBanner,
    redirectIfUnauthorized: redirectIfUnauthorized,
    isUnauthorizedError: isUnauthorizedError,
    readJsonOrThrow: readJsonOrThrow,
    UNAUTHORIZED_ERROR: UNAUTHORIZED_ERROR,
    DEFAULT_MESSAGE: DEFAULT_MESSAGE,
  };

  if (typeof window !== 'undefined') {
    window.AjaxStatus = AjaxStatus;
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = AjaxStatus;
  }
})();
