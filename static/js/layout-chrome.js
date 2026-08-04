// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

// Shared chrome for every authenticated page: the debounced search-filter
// helpers the templates' inline on*= attributes call, the jump-to-page box,
// and the scroll-to-top button.
//
// Lived inline in layout.html, where no linter could see it. Moved verbatim -
// it carries no Jinja at all, and a classic <script src> in the same position
// executes in the same order, so the inline handlers keep resolving these off
// window exactly as before.
window.__searchDebounceTimers = window.__searchDebounceTimers || {};

window.scheduleSearchFilter = function(searchInputId, applyFilterFn, delayMs = 500) {
  const searchInput = document.getElementById(searchInputId);
  if (!searchInput) {
    return;
  }

  window.clearTimeout(window.__searchDebounceTimers[searchInputId]);
  window.__searchDebounceTimers[searchInputId] = window.setTimeout(() => {
    applyFilterFn();
  }, delayMs);
};

window.handleSearchKeydown = function(event, applyFilterFn) {
  if (event.key !== 'Enter') {
    return;
  }

  event.preventDefault();
  applyFilterFn();
};

// Search boxes only navigate on Enter or when the field is left (blur) -
// not on every keystroke - so typing doesn't reload the page/reset
// scroll and focus mid-word.
window.handleSearchBlur = function(event, applyFilterFn) {
  const params = new URLSearchParams(window.location.search);
  const currentQ = params.get('q') || '';
  if (event.target.value.trim() !== currentQ.trim()) {
    applyFilterFn();
  }
};

// _pagination.html's "Go to page" input - Enter navigates, clamped to
// [1, totalPages] so a stray typo can't request a nonexistent page.
window.handleJumpToPageKeydown = function(event, totalPages) {
  if (event.key !== 'Enter') {
    return;
  }
  event.preventDefault();

  const value = parseInt(event.target.value, 10);
  if (!Number.isInteger(value)) {
    return;
  }

  const page = Math.min(Math.max(value, 1), totalPages);

  // A page can register window.__paginationAjaxHandler (see
  // history.html) to handle the jump in place instead of navigating -
  // unset on every other page, so this falls through to the default.
  if (window.__paginationAjaxHandler) {
    window.__paginationAjaxHandler(page);
    return;
  }

  const params = new URLSearchParams(window.location.search);
  params.set('page', page);
  window.location = window.location.pathname + '?' + params.toString();
};

(function(){
  const badge = document.getElementById('version-badge');
  const textEl = document.getElementById('version-badge-text');

  function hideBadge(){
    badge.style.display = 'none';
  }
  function checkVersion(){
    fetch('/version_status').then(r=>r.json()).then(data=>{
      if(data && data.latest){
        textEl.textContent = `New version: ${data.latest} (you: ${data.current})`;
        badge.style.display = 'inline-flex';
      } else {
        hideBadge();
      }
    }).catch(()=>{});
  }

  checkVersion();
  setInterval(checkVersion, 15 * 60 * 1000);
})();

(function(){
  const statusPill = document.getElementById('listener-status-pill');

  let pollTimer = null;

  function updateListenerStatus() {
    fetch('/api/listener-status')
      .then(r => {
        if (r.status === 401) {
          // Session expired: stop polling, don't just hide the pill. The
          // throw below lands in the catch, and with nothing clearing the
          // interval this kept hitting the server every 10s for the life of
          // the tab - the exact leak dashboard-page.js's now-playing poll
          // already fixed. A background poll shouldn't yank the page away
          // mid-read, so it stops rather than navigates - the next click on
          // anything goes through the normal login redirect.
          if (statusPill) statusPill.style.display = 'none';
          if (pollTimer !== null) {
            clearInterval(pollTimer);
            pollTimer = null;
          }
          throw new Error('Not logged in');
        }
        return r.json();
      })
      .then(data => {
        if (!data || !data.status || !statusPill) return;

        const status = data.status.toUpperCase();
        statusPill.className = `status-pill status-${status.toLowerCase()}`;
        statusPill.title = `Sync Status: ${status.charAt(0) + status.slice(1).toLowerCase()}`;
        statusPill.style.display = 'inline-block';
      })
      .catch(() => {});
  }

  updateListenerStatus();
  pollTimer = setInterval(updateListenerStatus, 10 * 1000);
})();


(function(){
  const navToggle = document.getElementById('nav-toggle');
  const navMenu = document.getElementById('nav-menu');

  if (!navToggle || !navMenu) return;

  // Both class flips are purely visual, so the button has to say what it did
  // (see the aria-expanded note in layout.html). Driven off navMenu's own
  // class rather than a counter, so the link handler below - which closes the
  // menu without going through here - can reuse it and never disagree.
  function syncNavExpanded() {
    navToggle.setAttribute('aria-expanded', String(navMenu.classList.contains('active')));
  }

  navToggle.addEventListener('click', () => {
    navMenu.classList.toggle('active');
    navToggle.classList.toggle('active');
    syncNavExpanded();
  });

  navMenu.querySelectorAll('a').forEach(link => {
    link.addEventListener('click', () => {
      navMenu.classList.remove('active');
      navToggle.classList.remove('active');
      syncNavExpanded();
    });
  });
})();

// The track-cover fade-in and the scroll-to-top progress ring live in
// chrome-common.js: they're byte-identical on the public (share-link)
// layout, and the two copies are exactly the fix-lands-in-one hazard.
