// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

// What is left of the /history page's browser logic once htmx owns the
// request/swap layer (see templates/history.html for the attributes).
//
// This file used to be 225 lines. Everything that went is something htmx does
// declaratively, and the mapping is worth keeping written down, because the
// next page to migrate deletes the same five things:
//
//   loadHistoryResults + AbortController  -> hx-get + hx-sync="...:replace"
//   replaceHistoryUrl                     -> hx-replace-url="true"
//   the debounce helpers on the search box-> hx-trigger "input changed delay:400ms"
//   the delegated pagination click handler-> hx-boost on the pagination wrapper
//   the 401 -> /login branch              -> HX-Redirect, sent by the server
//                                            (see app.py unauthenticatedResponse)
//   the popstate handler                  -> nothing, and deliberately: every
//     URL update here REPLACES, so this page never puts an entry on the history
//     stack for itself and there is no in-page state to pop back to. The
//     handler only ever ran on a cross-document Back, which reloads the page
//     server-side anyway.
//
// What genuinely could not move is below: a date range needs validating before
// it is worth a request, the sort toggle has no natural form control, and the
// jump-to-page input is an <input> rather than a link so hx-boost cannot see
// it. Note there is no `hx-on:` or event-filter equivalent available as a
// shortcut here - the CSP withholds 'unsafe-eval' from this page (see the
// header comment in templates/history.html).

//< the form htmx watches; also the element the sort toggle re-triggers
var HISTORY_FORM_ID = 'historyFilters';
//< the swap target it fills, in history.html
var HISTORY_RESULTS_ID = 'historyResults';

// The date-range check, its error display, the custom-range show/hide and the
// empty-param pruning all live in static/js/htmx-filters.js, shared with the
// four other filter pages - they carry the same control set, and five copies of
// "is this range worth a request" would eventually disagree. Loaded before this
// file (see templates/history.html).
var RANGE_OK = HtmxFilters.RANGE_OK;

if (typeof document !== 'undefined') {
  var byId = function (id) { return document.getElementById(id); };

  // Called from the Time Period select's onchange. Runs before htmx's own
  // listener does (an inline on*= handler fires at the target, htmx's is on the
  // form and fires as the event bubbles), so the disabled flags below are
  // already correct by the time the request is serialized.
  //
  // `disabled`, not merely hidden: a disabled control is not serialized, which
  // is what keeps a stale custom range out of the request - and therefore out
  // of the URL - after switching back to a named interval.
  window.updateHistoryInterval = function () { HtmxFilters.syncCustomRange('historyCustomDates'); };

  // The Date sort toggle: flips newest-first (default) <-> oldest-first. The
  // value lives in a hidden form field so htmx builds the query string from one
  // place; this only has to flip it, relabel the button and tell the form to
  // re-fire. Resetting to page 1 is implicit - `page` is not a form field, so
  // serializing the form drops it.
  window.updateHistorySort = function () {
    var field = byId('historySortValue');
    var next = field.value === 'oldest' ? '' : 'oldest';
    field.value = next;
    byId('historySort').textContent = next === 'oldest' ? 'Date ↑' : 'Date ↓';
    byId(HISTORY_FORM_ID).dispatchEvent(new Event('historyRefresh'));
  };

  // _pagination.html's jump-to-page input calls the shared
  // handleJumpToPageKeydown (static/js/layout-chrome.js), which defers to this
  // hook when present instead of navigating. It is an <input>, not a link, so
  // hx-boost does not cover it the way it covers Prev/Next.
  //
  // replaceState, never push - the same rule the hx-replace-url attributes
  // encode, and tests/test_pagination_ajax_handler.py asserts for this file.
  // htmx.ajax has no replace-url option of its own, so the URL is updated here
  // and the swap requested separately.
  var goToHistoryPage = function (page) {
    var params = new URLSearchParams(window.location.search);
    params.set('page', page);
    var url = window.location.pathname + '?' + params.toString();
    window.history.replaceState({}, '', url);
    htmx.ajax('GET', url, { target: '#' + HISTORY_RESULTS_ID, swap: 'innerHTML' });
  };
  window.__paginationAjaxHandler = goToHistoryPage;

  // The one place a request gets vetoed. Scoped to requests the FORM makes:
  // a boosted pagination link carries its whole query in its href and must keep
  // working even while the Time Period select sits on a half-entered custom
  // range, which is exactly the state that blocks a form request.
  document.body.addEventListener('htmx:configRequest', function (evt) {
    if (!evt.detail.elt || evt.detail.elt.id !== HISTORY_FORM_ID) return;
    var problem = HtmxFilters.rangeProblemFromDom();
    HtmxFilters.showRangeError(problem);
    if (problem !== RANGE_OK) {
      evt.preventDefault();
      return;
    }
    HtmxFilters.pruneEmptyParams(evt.detail.parameters);
  });

  //< cover-art fade-ins are handled once for the whole app in
  //  static/js/chrome-common.js, which already owned this behaviour and now
  //  re-runs its sweep on htmx:afterSwap

  // A genuine failure gets the shared inline error + Retry rather than a stuck
  // "Loading…" or a silently stale list. An expired session no longer arrives
  // here at all: the server answers an htmx request with HX-Redirect, so the
  // browser navigates to /login instead of this reporting a load failure.
  HtmxFilters.onSwapFailure(HISTORY_RESULTS_ID, function () {
    htmx.ajax('GET', window.location.pathname + window.location.search,
              { target: '#' + HISTORY_RESULTS_ID, swap: 'innerHTML' });
  });
}
//< no module.exports: everything pure moved to static/js/htmx-filters.js, which
//  is where the plain-node unit test now points (tests/test_htmx_filters.js)
