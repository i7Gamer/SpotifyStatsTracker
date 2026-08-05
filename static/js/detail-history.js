// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

/* What is left of the song/artist/album detail pages' play-history list once
 * htmx owns the request/swap layer (see templates/_detail_history_container.html
 * for the wiring, and _play_log_batch.html for the "Show more" control).
 *
 * This file used to be 244 lines, and everything that went is something htmx
 * does declaratively - the same five deletions /history and the Top lists made,
 * plus one this page had on its own:
 *
 *   loadDetailHistory + AbortController -> hx-get + hx-sync="...:replace"
 *   the replaceState bookkeeping        -> hx-replace-url="true"
 *   the delegated click handler for the -> hx-boost on the control group and on
 *     sort/skips toggles and pagination    the pagination wrapper
 *   the 401 -> /login branch            -> HX-Redirect, sent by the server
 *   the popstate handler                -> nothing, and deliberately: every URL
 *     update here REPLACES, so this page never puts an entry on the history
 *     stack for itself and there is nothing to pop back to. It only ever ran on
 *     a cross-document Back, which reloads the page server-side anyway.
 *   the "Show more" fetch + the append  -> hx-target="#timelineActions" +
 *     + the listGeneration counter         hx-swap="outerHTML" on the button,
 *     which sits at the END of the list, so the batch that replaces it lands
 *     its rows exactly where the append used to put them. The counter went with
 *     it: the button inherits the container's hx-sync, so a sort change aborts
 *     a batch in flight rather than letting stale rows land in a fresh list.
 *
 * What could not move: the Top Songs / History tabs (a click that changes no
 * data, so there is no request for htmx to own), and the jump-to-page input,
 * which is an <input> rather than a link so hx-boost cannot see it. Both bind
 * to markup that arrives with the deferred body, so the wiring lives in
 * initDetailHistory() and detail-page.js calls it after each body swap - at
 * script-load time none of these elements exist yet. */
(function () {
  //< the swap target the boosted controls fill; also what tells this module's
  //  failure banner apart from the whole-body one (see detail-page.js)
  var HISTORY_RESULTS_ID = 'detailHistoryResults';

  //< re-resolved by initDetailHistory on every body swap; empty until then
  var filterButtons = [];
  var categoryDivs = [];

  // The tabs flip visibility over content that is already on the page - no
  // request, so nothing for htmx to own. The URL still has to follow, and it
  // replaceStates for the same reason every other update here does: Back must
  // leave the detail page rather than step back through its tab states.
  function activateView(view, replaceUrl) {
    filterButtons.forEach(function (btn) { btn.classList.toggle('active', btn.dataset.filter === view); });
    categoryDivs.forEach(function (div) { div.classList.toggle('visible', div.dataset.category === view); });
    if (replaceUrl) {
      var params = new URLSearchParams(window.location.search);
      if (view === 'top-songs') { params.delete('view'); } else { params.set('view', view); }
      var query = params.toString();
      window.history.replaceState({}, '', window.location.pathname + (query ? '?' + query : ''));
    }
  }

  // _pagination.html's "Go to page" input calls the shared
  // handleJumpToPageKeydown (layout-chrome.js), which hands the page over to
  // this hook when one is registered and otherwise navigates with a full
  // reload. It is an <input>, not a link, so hx-boost does not cover it the way
  // it covers the Prev/Next links printed beside it by the same include.
  //
  // replaceState, never push - the same rule the hx-replace-url attributes
  // encode, and tests/test_pagination_ajax_handler.py asserts for this file.
  // htmx.ajax has no replace-url option of its own, so the URL is updated here
  // and the swap requested separately.
  function goToDetailHistoryPage(page) {
    var params = new URLSearchParams(window.location.search);
    params.set('page', page);
    var url = window.location.pathname + '?' + params.toString();
    window.history.replaceState({}, '', url);
    htmx.ajax('GET', url, { target: '#' + HISTORY_RESULTS_ID, swap: 'innerHTML' });
  }

  // Called by detail-page.js after each body swap. Every element referenced
  // here is replaced wholesale by that swap, so re-resolving them is also what
  // keeps the listeners from stacking - the nodes they were bound to are gone.
  function initDetailHistory() {
    filterButtons = document.querySelectorAll('.stats-filter-button');
    categoryDivs = document.querySelectorAll('[data-category]');
    filterButtons.forEach(function (button) {
      button.addEventListener('click', function () { activateView(button.dataset.filter, true); });
    });

    //< here rather than at module load: until the deferred body arrives there
    //  is no list to swap, and the full-reload fallback is right then
    if (document.getElementById(HISTORY_RESULTS_ID)) {
      window.__paginationAjaxHandler = goToDetailHistoryPage;
    }
  }

  // The list's own swaps, as opposed to the whole body's. Both fire on
  // document.body, and the container is INSIDE #detailBody, so "is the swap
  // target this container or something in it" is what tells them apart - a
  // failed sort must not also blank the body, which detail-page.js would do if
  // it saw the same event.
  function isHistorySwap(target) {
    var results = document.getElementById(HISTORY_RESULTS_ID);
    return !!results && !!target && (target === results || results.contains(target));
  }

  // A failed sort/page/batch request gets a banner rather than renderInto: the
  // list still holds the previous page, which is worth keeping on screen behind
  // the error.
  function reportHistoryFailure(evt) {
    if (!window.AjaxStatus || !evt.detail || !isHistorySwap(evt.detail.target)) return;
    window.AjaxStatus.showBanner(function () {
      htmx.ajax('GET', window.location.pathname + window.location.search,
                { target: '#' + HISTORY_RESULTS_ID, swap: 'innerHTML' });
    });
  }

  if (typeof window !== 'undefined') {
    window.initDetailHistory = initDetailHistory;
    document.body.addEventListener('htmx:responseError', reportHistoryFailure);
    document.body.addEventListener('htmx:sendError', reportHistoryFailure);
    //< a swap that succeeded clears whatever the last failure left up
    document.body.addEventListener('htmx:afterSwap', function (evt) {
      if (window.AjaxStatus && isHistorySwap(evt.target)) window.AjaxStatus.clearBanner();
    });
  }
})();
