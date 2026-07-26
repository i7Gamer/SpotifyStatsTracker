// Shared "back" button behavior for song/album/artist detail pages.
// Labels the button after the page the user actually came from (via
// document.referrer) and, whenever that referrer is same-origin, navigates
// with history.back() instead of a fresh link so filters/pagination/scroll
// position on the previous page are preserved.
//
// When the page was opened in a *new* tab there is an in-app referrer but no
// history entry behind it, so history.back() would silently do nothing - the
// button is hidden in that case rather than left dead.

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
// Returns null when there is no usable in-app referrer (direct link,
// external site) - callers should keep the server-rendered default
// href/label in that case - { hide: true } when the referrer is in-app but
// unreachable via history.back(), and { hide: false, label } otherwise.
function resolveBackTarget(referrer, currentOrigin, canGoBack) {
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

  if (!canGoBack) {
    return { hide: true };
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
