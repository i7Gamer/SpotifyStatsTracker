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

  //< the Trend-buckets chart (detail-chart.js) reports through the same banner
  //  slot; naming this list keeps one's success from clearing the other's failure
  var BANNER_OWNER = 'detail-history';

  //< re-resolved by initDetailHistory on every body swap; empty until then
  var filterButtons = [];
  var categoryDivs = [];

  var SHOW_MORE_BUTTON_ID = 'showMorePlaysBtn';

  // "Show More Plays" (templates/_play_log_batch.html) carries
  // hx-disabled-elt="this": htmx disables it right after htmx:beforeRequest,
  // which blurs it, so by beforeSwap/afterSettle nothing inside the swapped
  // #timelineActions holds focus any more (the shared helper in
  // htmx-filters.js sees nothing to restore). The swap is also outerHTML, so
  // the old button - and the old #timelineActions - are gone by afterSettle;
  // there is no by-id match for htmx's own restore either, because the old
  // button was already disabled (and therefore unfocused) before the swap.
  // Recorded here instead: true only when the button itself held focus right
  // before the request that is about to disable it.
  var restoreShowMoreFocus = false;

  // htmx fires beforeRequest BEFORE hx-disabled-elt disables (and blurs) the
  // button - the last point document.activeElement can still be it.
  function armShowMoreFocusRestore(evt) {
    var elt = evt && evt.detail && evt.detail.elt;
    restoreShowMoreFocus = !!(elt && elt.id === SHOW_MORE_BUTTON_ID
      && document.activeElement === elt);
  }

  // The button inherits the list's hx-sync="#detailHistoryResults:replace",
  // so a sort/skips/pagination change firing while a batch is in flight
  // aborts that batch rather than queuing behind it. None of these fire for
  // the OTHER swap that then lands (a sort's own beforeRequest re-arms the
  // flag for itself if it applies), so clearing here only ever disarms a
  // batch that will never reach afterSettle - never a later request's own
  // arm. Deliberately NOT htmx:afterRequest: it fires before the deferred
  // afterSettle on a SUCCESSFUL request too, so clearing there would disarm
  // every batch before its own afterSettle ever got a chance to look at the
  // flag.
  function disarmShowMoreFocusRestore() {
    restoreShowMoreFocus = false;
  }

  function restoreShowMoreFocusIfArmed() {
    if (!restoreShowMoreFocus) return;
    restoreShowMoreFocus = false;
    var newButton = document.getElementById(SHOW_MORE_BUTTON_ID);
    if (newButton) {
      newButton.focus();
      return;
    }
    //< the batch that just landed was the last one: no new button to hand
    //  focus to, so fall back to the container - the pattern the shared
    //  helper (htmx-filters.js) uses for the same situation elsewhere
    var results = document.getElementById(HISTORY_RESULTS_ID);
    if (!results) return;
    if (!results.hasAttribute || !results.hasAttribute('tabindex')) {
      if (results.setAttribute) results.setAttribute('tabindex', '-1');
    }
    results.focus();
  }

  // The tabs flip visibility over content that is already on the page - no
  // request, so nothing for htmx to own. The URL still has to follow, and it
  // replaceStates for the same reason every other update here does: Back must
  // leave the detail page rather than step back through its tab states.
  function activateView(view, replaceUrl) {
    filterButtons.forEach(function (btn) {
      var pressed = btn.dataset.filter === view;
      btn.classList.toggle('active', pressed);
      //< aria-pressed moves with the class: the pressed tab is otherwise
      //  colour alone, and a toggle button's state is what a screen reader reads
      btn.setAttribute('aria-pressed', String(pressed));
    });
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
  // htmx does the replacing: the `replace` option is forwarded into the same
  // history update hx-replace-url feeds (a path is used as given; only "true"
  // means "the request path"), and that update runs only inside the
  // successful-swap branch. The address bar used to be rewritten here, BEFORE
  // the request, so a failed jump left it claiming a page the list never
  // showed. Issued off the list so the request inherits its hx-target /
  // hx-swap / hx-sync, and a jump during an in-flight sort change is
  // serialised like every other swap into it.
  function goToDetailHistoryPage(page) {
    var params = new URLSearchParams(window.location.search);
    params.set('page', page);
    var url = window.location.pathname + '?' + params.toString();
    htmx.ajax('GET', url, { source: document.getElementById(HISTORY_RESULTS_ID), replace: url });
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
    }, undefined, BANNER_OWNER);
  }

  if (typeof window !== 'undefined') {
    window.initDetailHistory = initDetailHistory;
    document.body.addEventListener('htmx:responseError', reportHistoryFailure);
    document.body.addEventListener('htmx:sendError', reportHistoryFailure);
    //< a swap that succeeded clears what this list's last failure left up -
    //  and only that: the chart's banner is its own to clear
    document.body.addEventListener('htmx:afterSwap', function (evt) {
      if (window.AjaxStatus && isHistorySwap(evt.target)) window.AjaxStatus.clearBanner(BANNER_OWNER);
    });

    document.body.addEventListener('htmx:beforeRequest', armShowMoreFocusRestore);
    document.body.addEventListener('htmx:afterSettle', restoreShowMoreFocusIfArmed);
    //< a batch that never lands must not leave a stale arm for some later,
    //  unrelated settle to act on (see disarmShowMoreFocusRestore above)
    document.body.addEventListener('htmx:responseError', disarmShowMoreFocusRestore);
    document.body.addEventListener('htmx:sendError', disarmShowMoreFocusRestore);
    document.body.addEventListener('htmx:sendAbort', disarmShowMoreFocusRestore);
    document.body.addEventListener('htmx:timeout', disarmShowMoreFocusRestore);
  }
})();
