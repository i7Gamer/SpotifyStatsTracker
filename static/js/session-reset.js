// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

/* Drop htmx's page-snapshot cache when the login page is reached.
 *
 * htmx keeps a snapshot of each history-managed page - the innerHTML of the
 * history element, which is document.body here - so a Back navigation can be
 * restored without a request. It lives in sessionStorage under
 * 'htmx-history-cache', and nothing clears it on logout, because logging out is
 * a server-side act and sessionStorage is not.
 *
 * So on a shared browser: log out, land here, press Back, and the previous
 * account's dashboard is painted straight from storage with no request made and
 * therefore no session to check. The data is a frozen snapshot rather than live
 * - every htmx request from it now redirects to login - but it is still the
 * previous user's listening history on screen after they logged out.
 *
 * Clearing it HERE rather than disabling the cache is the narrow fix. htmx has
 * no "cache off" switch that keeps restoring working: historyCacheSize = 0
 * removes the key and makes every Back a cache MISS, which is a server
 * re-fetch on every Back rather than no caching. The login page is where a
 * session ends and where the next one begins, so it is where a snapshot from
 * the last one stops being anyone's to see.
 *
 * A cache miss after this is handled correctly - see routes/_htmx.py, which
 * answers a history-restore request with a whole page. Logged out, that is the
 * login page, which is the honest thing to show.
 */

//< htmx's own key; it is not exported by the library, so it is named here
var HTMX_HISTORY_CACHE_KEY = 'htmx-history-cache';

// Takes the storage rather than reaching for sessionStorage, so the node unit
// test can hand it a stub (tests/test_session_reset.js).
//
// Storage throws rather than returning null when it is unavailable - Safari's
// private mode historically, and any browser configured to block site data - and
// this runs on the page someone is trying to log in on, so a throw here would
// break the login form over a cache that may not even exist.
function clearHtmxHistoryCache(storage) {
  try {
    storage.removeItem(HTMX_HISTORY_CACHE_KEY);
    return true;
  } catch (err) {
    return false;
  }
}

if (typeof window !== 'undefined' && window.sessionStorage) {
  clearHtmxHistoryCache(window.sessionStorage);
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { clearHtmxHistoryCache, HTMX_HISTORY_CACHE_KEY };
}
