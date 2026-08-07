// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

/* What is left of the Top Songs/Artists/Albums pages' browser logic once htmx
 * owns the request/swap layer (see templates/_page_card.html for the wiring,
 * shared by all three pages).
 *
 * This file was 208 lines of the same loader /history had: an AbortController
 * to stop a superseded response landing, replaceState bookkeeping, a delegated
 * pagination click listener, and four filter handlers that each hand-edited the
 * query string. htmx does all of that declaratively, and the pure "which params
 * does this filter set vs delete" helpers went with it - the filter card now IS
 * the parameter list, serialized by htmx.
 *
 * What could not move is below, and it is now the same short list as /history:
 * the jump-to-page hook and the request veto. The "Full plays only" hidden
 * field used to make it one longer, and moved to static/js/htmx-filters.js when
 * /history started rendering the same partial.
 *
 * Note there is no `hx-on:` or event-filter shortcut available here: the CSP
 * withholds 'unsafe-eval' from these pages (see templates/_page_card.html). */

//< the form htmx watches, in _page_card.html
var TOP_LIST_FORM_ID = 'topListFilters';
//< the swap target it fills, in _top_list_container.html
var TOP_LIST_RESULTS_ID = 'topListResults';

if (typeof document !== 'undefined') {
  // Called from the Time Period select's onchange. Runs before htmx's listener
  // (an inline on*= handler fires at the target; htmx's is on the form and fires
  // as the event bubbles), so the disabled flags are already right by the time
  // the request is serialized.
  //
  // `disabled`, not merely hidden: a disabled control is not serialized, which
  // is what keeps a stale custom range out of the request - and so out of the
  // URL - after switching back to a named interval.
  window.updateIntervalFilter = function () { HtmxFilters.syncCustomRange('customDates'); };

  //< "Full plays only" moved to static/js/htmx-filters.js, which exports
  //  window.updateFullPlaysFilter itself: /history renders the same partial now
  //  (templates/_full_plays_toggle.html) and does not load this file

  // _pagination.html's jump-to-page input calls the shared
  // handleJumpToPageKeydown (static/js/layout-chrome.js), which defers to this
  // hook when present. It is an <input>, not a link, so hx-boost does not cover
  // it the way it covers Prev/Next.
  //
  // replaceState, never push - the same rule the hx-replace-url attributes
  // encode, and tests/test_pagination_ajax_handler.py asserts for this file.
  // htmx.ajax has no replace-url option, so the URL is updated here and the swap
  // requested separately.
  var goToTopListPage = function (page) {
    var params = new URLSearchParams(window.location.search);
    params.set('page', page);
    var url = window.location.pathname + '?' + params.toString();
    window.history.replaceState({}, '', url);
    htmx.ajax('GET', url, { target: '#' + TOP_LIST_RESULTS_ID, swap: 'innerHTML' });
  };
  window.__paginationAjaxHandler = goToTopListPage;

  // The one place a request gets vetoed. Scoped to requests the FORM makes: a
  // boosted pagination link carries its whole query in its href and must keep
  // working even while the Time Period select sits on a half-entered custom
  // range, which is exactly the state that blocks a form request.
  document.body.addEventListener('htmx:configRequest', function (evt) {
    if (!evt.detail.elt || evt.detail.elt.id !== TOP_LIST_FORM_ID) return;
    var problem = HtmxFilters.rangeProblemFromDom();
    HtmxFilters.showRangeError(problem);
    if (problem !== HtmxFilters.RANGE_OK) {
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
  HtmxFilters.onSwapFailure(TOP_LIST_RESULTS_ID, function () {
    htmx.ajax('GET', window.location.pathname + window.location.search,
              { target: '#' + TOP_LIST_RESULTS_ID, swap: 'innerHTML' });
  });
}
//< no module.exports: everything pure moved to static/js/htmx-filters.js, which
//  is where the plain-node unit test now points (tests/test_htmx_filters.js)
