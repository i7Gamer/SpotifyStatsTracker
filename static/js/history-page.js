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

// The three states a Time Period selection can be in. Split out as a pure
// function (unit-tested in tests/test_history_page.js) because two different
// callers need the same answer: the error display, and the gate that decides
// whether a request is worth sending at all.
var RANGE_OK = null;
var RANGE_INCOMPLETE = 'incomplete';
var RANGE_INVERTED = 'inverted';
var RANGE_INVERTED_MESSAGE = 'Start date cannot be after end date.';

function historyRangeProblem(interval, startDate, endDate) {
  //< only a custom range can be malformed; a named interval needs no dates
  if (interval !== 'custom') return RANGE_OK;
  //< half-entered, which is what every keystroke of typing a date looks like:
  //  not an error to shout about, but not something to query on either
  if (!startDate || !endDate) return RANGE_INCOMPLETE;
  if (new Date(startDate) > new Date(endDate)) return RANGE_INVERTED;
  return RANGE_OK;
}

// Drop the params the user has not set. htmx serializes every enabled control
// in the form, so an untouched search box or tag dropdown would otherwise put
// `q=&tag=` in the request - and hx-replace-url writes the requested URL back
// to the address bar, so those empties become part of the link people copy.
//
// Takes the parameters object htmx hands to htmx:configRequest, which is a
// Proxy over a FormData with deleteProperty and ownKeys traps - so ordinary
// object operations work on it, and a plain object works in the unit test.
function pruneEmptyParams(parameters) {
  Object.keys(parameters).forEach(function (key) {
    if (parameters[key] === '') delete parameters[key];
  });
  return parameters;
}

// Everything below only runs in a browser (guarded on `document`, like
// static/js/top-list.js's init) so this module can still be `require()`d from
// a plain-node unit test for its pure helpers above.
if (typeof document !== 'undefined') {
  var byId = function (id) { return document.getElementById(id); };

  var currentRangeProblem = function () {
    return historyRangeProblem(byId('interval').value, byId('startDate').value, byId('endDate').value);
  };

  var showRangeError = function (problem) {
    var invalid = problem === RANGE_INVERTED;
    var errorElem = byId('dateError');
    errorElem.textContent = invalid ? RANGE_INVERTED_MESSAGE : '';
    errorElem.style.display = invalid ? 'block' : 'none';
    byId('startDate').style.borderColor = invalid ? 'var(--accent)' : '';
    byId('endDate').style.borderColor = invalid ? 'var(--accent)' : '';
  };

  // Called from the Time Period select's onchange. Runs before htmx's own
  // listener does (an inline on*= handler fires at the target, htmx's is on the
  // form and fires as the event bubbles), so the disabled flags below are
  // already correct by the time the request is serialized.
  //
  // `disabled`, not merely hidden: a disabled control is not serialized, which
  // is what keeps a stale custom range out of the request - and therefore out
  // of the URL - after switching back to a named interval.
  window.updateHistoryInterval = function () {
    var custom = byId('interval').value === 'custom';
    byId('historyCustomDates').style.display = custom ? 'flex' : 'none';
    byId('startDate').disabled = !custom;
    byId('endDate').disabled = !custom;
    showRangeError(currentRangeProblem());
  };

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
    htmx.ajax('GET', url, { target: '#historyResults', swap: 'innerHTML' });
  };
  window.__paginationAjaxHandler = goToHistoryPage;

  // The one place a request gets vetoed. Scoped to requests the FORM makes:
  // a boosted pagination link carries its whole query in its href and must keep
  // working even while the Time Period select sits on a half-entered custom
  // range, which is exactly the state that blocks a form request.
  document.body.addEventListener('htmx:configRequest', function (evt) {
    if (!evt.detail.elt || evt.detail.elt.id !== HISTORY_FORM_ID) return;
    var problem = currentRangeProblem();
    showRangeError(problem);
    if (problem !== RANGE_OK) {
      evt.preventDefault();
      return;
    }
    pruneEmptyParams(evt.detail.parameters);
  });

  // Smooth cover-art fade-ins for freshly swapped cards. The images are new
  // elements on every swap, so this cannot be a one-time delegated listener.
  document.body.addEventListener('htmx:afterSwap', function (evt) {
    if (evt.target.id !== 'historyResults') return;
    document.querySelectorAll('#historyResults img.track-cover').forEach(function (img) {
      if (img.complete) {
        img.classList.add('loaded');
      } else {
        img.addEventListener('load', function () { img.classList.add('loaded'); });
      }
    });
  });

  // A genuine failure gets the shared inline error + Retry rather than a stuck
  // "Loading…" or a silently stale list. An expired session no longer arrives
  // here at all: the server answers an htmx request with HX-Redirect, so the
  // browser navigates to /login instead of this reporting a load failure.
  var reportHistoryFailure = function () {
    var target = byId('historyResults');
    if (!target || !window.AjaxStatus) return;
    window.AjaxStatus.renderInto(target, function () {
      htmx.ajax('GET', window.location.pathname + window.location.search,
                { target: '#historyResults', swap: 'innerHTML' });
    });
  };
  document.body.addEventListener('htmx:responseError', reportHistoryFailure);
  document.body.addEventListener('htmx:sendError', reportHistoryFailure);
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    historyRangeProblem,
    pruneEmptyParams,
    RANGE_OK,
    RANGE_INCOMPLETE,
    RANGE_INVERTED,
  };
}
