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

// A tab that has only ever shown this one page has a history length of 1.
const MIN_HISTORY_LENGTH_WITH_BACK_ENTRY = 1;

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

// Pure decision function: given the referrer, the current page's origin and
// whether the tab can go back at all, decide what to do with the button.
// Returns { hide: true } when this tab has nowhere to go back to, null when
// it does but the referrer is unusable (external site, no referrer) -
// callers should keep the server-rendered default href/label in that case -
// and { hide: false, label } otherwise.
function resolveBackTarget(referrer, currentOrigin, canGoBack) {
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

  if (referrerUrl.origin !== currentOrigin) {
    return null;
  }

  const match = BACK_BUTTON_PATH_LABELS.find((entry) => entry.test(referrerUrl.pathname));
  return { hide: false, label: match ? `← Back to ${match.label}` : null };
}

function initBackButton() {
  const backButton = document.getElementById('back-button');
  if (!backButton) {
    return;
  }

  const canGoBack = hasEarlierHistoryEntry(window.navigation, window.history.length);
  const target = resolveBackTarget(document.referrer, window.location.origin, canGoBack);
  if (!target) {
    return;
  }

  if (target.hide) {
    backButton.hidden = true;
    return;
  }

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
  module.exports = { resolveBackTarget, hasEarlierHistoryEntry, BACK_BUTTON_PATH_LABELS };
}
