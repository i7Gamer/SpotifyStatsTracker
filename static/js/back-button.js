// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

// Shared "back" button behavior for song/album/artist detail pages.
// Labels the button after the page the user actually came from (via
// document.referrer) and, whenever that referrer is same-origin, navigates
// with history.back() instead of a fresh link so filters/pagination/scroll
// position on the previous page are preserved.
//
// When the page was opened in a *new* tab there is no history entry behind it,
// so history.back() would silently do nothing and the server-rendered fallback
// href is only a guess at a page the user never visited - the button is hidden
// in that case rather than left dead or misleading. That holds however the tab
// was opened: ctrl/middle-click from inside the app (in-app referrer), a shared
// link from another site (cross-origin referrer), or a pasted URL or bookmark
// (no referrer at all).
//
// One referrer is not a page the visitor came FROM: this page itself, left
// behind by a link that pointed back at the page it was on. That reload keeps
// the same history entry, so the label has to survive it too - it is remembered
// in sessionStorage and carried over rather than re-derived. See
// resolveBackTarget.

// A tab that has only ever shown this one page has a history length of 1.
const MIN_HISTORY_LENGTH_WITH_BACK_ENTRY = 1;

// Where a load's resolved label is remembered for a later reload of the SAME
// page (see resolveBackTarget's same-page branch). sessionStorage scopes it to
// this tab; the key carries the page's own URL so two detail pages open in one
// tab can't read each other's.
const BACK_BUTTON_LABEL_STORAGE_PREFIX = 'backButtonLabel:';

// Remembered when the referrer resolved to no label of its own - "go back, but
// keep the server-rendered text". Distinct from nothing stored at all, which
// means no earlier load of this page ever got that far.
const NO_LABEL_REMEMBERED = '';

const BACK_BUTTON_PATH_LABELS = [
  { test: (pathname) => pathname === '/', label: 'Dashboard' },
  { test: (pathname) => pathname === '/history', label: 'History' },
  { test: (pathname) => pathname === '/wrapped', label: 'Wrapped' },
  { test: (pathname) => pathname === '/genres', label: 'Genres' },
  { test: (pathname) => pathname === '/compare', label: 'Compare' },
  { test: (pathname) => pathname === '/top-songs', label: 'Top Songs' },
  { test: (pathname) => pathname === '/top-albums', label: 'Top Albums' },
  { test: (pathname) => pathname === '/top-artists', label: 'Top Artists' },
  { test: (pathname) => pathname.startsWith('/song/'), label: 'Song' },
  { test: (pathname) => pathname.startsWith('/album/'), label: 'Album' },
  { test: (pathname) => pathname.startsWith('/artist/'), label: 'Artist' },
];

// Does this tab have an entry to go back to? Chrome's Navigation API answers
// it exactly; everywhere else a history length of 1 means this page is the
// tab's only entry (opened in a new tab, bookmark, pasted URL).
function hasEarlierHistoryEntry(navigationApi, historyLength) {
  if (navigationApi && typeof navigationApi.canGoBack === 'boolean') {
    return navigationApi.canGoBack;
  }
  return historyLength > MIN_HISTORY_LENGTH_WITH_BACK_ENTRY;
}

// The identity a referrer has to match to count as "the page you are already
// on": origin, path and query. The fragment is left out - a referrer never
// carries one, and a fragment navigation doesn't reload the page anyway.
function pageIdentity(url) {
  return url.origin + url.pathname + url.search;
}

// Key under which this page's resolved label is remembered.
function backButtonLabelStorageKey(currentUrl) {
  return BACK_BUTTON_LABEL_STORAGE_PREFIX + pageIdentity(new URL(currentUrl));
}

// Pure decision function: given the referrer, the current page's URL, whether
// the tab can go back at all and the label a previous load of this same page
// remembered, decide what to do with the button. Returns { hide: true } when
// this tab has nowhere to go back to, null when it does but the referrer is
// unusable (external site, no referrer, or a stay on this page with nothing
// remembered) - callers should keep the server-rendered default href/label in
// that case - and { hide: false, label } otherwise.
function resolveBackTarget(referrer, currentUrl, canGoBack, rememberedLabel) {
  // Checked before the referrer: a tab with no earlier entry has nothing to go
  // back to no matter where the visit came from, so this decides on its own.
  if (!canGoBack) {
    return { hide: true };
  }

  if (!referrer) {
    return null;
  }

  let referrerUrl;
  try {
    referrerUrl = new URL(referrer);
  } catch (e) {
    return null;
  }

  const current = new URL(currentUrl);
  if (referrerUrl.origin !== current.origin) {
    return null;
  }

  // Clicking a link to the page you are already on - a song list linking its
  // own artist, a track card linking the album you are looking at - reloads
  // this page with ITSELF as the referrer, and the browser replaces the tab's
  // current history entry rather than adding one. So Back still leads exactly
  // where it led before the click, and re-deriving the label here would rename
  // the button after the page the visitor never left ("← Back to Artist" while
  // standing on that artist). What the previous load resolved is still true.
  if (pageIdentity(referrerUrl) === pageIdentity(current)) {
    if (rememberedLabel === null || rememberedLabel === undefined) {
      return null;
    }
    return { hide: false, label: rememberedLabel || null };
  }

  const match = BACK_BUTTON_PATH_LABELS.find((entry) => entry.test(referrerUrl.pathname));
  return { hide: false, label: match ? `← Back to ${match.label}` : null };
}

// sessionStorage can throw outright (Safari private browsing, storage disabled
// by policy). Losing the memory only costs the carry-over above, so both sides
// degrade to "nothing remembered" rather than taking the button down with them.
function readRememberedLabel(key) {
  try {
    return window.sessionStorage.getItem(key);
  } catch (e) {
    return null;
  }
}

function rememberLabel(key, label) {
  try {
    window.sessionStorage.setItem(key, label);
  } catch (e) {
    //< nothing to do: the next same-page click just falls back to the default
  }
}

function initBackButton() {
  const backButton = document.getElementById('back-button');
  if (!backButton) {
    return;
  }

  const storageKey = backButtonLabelStorageKey(window.location.href);
  const canGoBack = hasEarlierHistoryEntry(window.navigation, window.history.length);
  const target = resolveBackTarget(document.referrer, window.location.href, canGoBack,
                                   readRememberedLabel(storageKey));
  if (!target) {
    return;
  }

  if (target.hide) {
    backButton.hidden = true;
    return;
  }

  //< for a later click on a link back to this same page, which reloads it with
  //  itself as the referrer - see resolveBackTarget
  rememberLabel(storageKey, target.label || NO_LABEL_REMEMBERED);

  if (target.label) {
    backButton.textContent = target.label;
  }
  backButton.href = '#';
  backButton.onclick = (e) => {
    e.preventDefault();
    history.back();
  };
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    resolveBackTarget, hasEarlierHistoryEntry, backButtonLabelStorageKey, initBackButton,
    BACK_BUTTON_PATH_LABELS,
  };
}
