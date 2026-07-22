// Shared "back" button behavior for song/album/artist detail pages.
// Labels the button after the page the user actually came from (via
// document.referrer) and, whenever that referrer is same-origin, navigates
// with history.back() instead of a fresh link so filters/pagination/scroll
// position on the previous page are preserved.

const BACK_BUTTON_PATH_LABELS = [
  { test: (pathname) => pathname === '/', label: 'Dashboard' },
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

// Pure decision function: given the referrer and the current page's origin,
// decide whether we can reliably navigate back and what to call it.
// Returns null when there is no usable in-app referrer (direct link, new
// tab, external site) - callers should keep the server-rendered default
// href/label in that case.
function resolveBackTarget(referrer, currentOrigin) {
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
  return { label: match ? `← Back to ${match.label}` : null };
}

function initBackButton() {
  const backButton = document.getElementById('back-button');
  if (!backButton) {
    return;
  }

  const target = resolveBackTarget(document.referrer, window.location.origin);
  if (!target) {
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
  module.exports = { resolveBackTarget, BACK_BUTTON_PATH_LABELS };
}
