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

  function updateListenerStatus() {
    fetch('/api/listener-status')
      .then(r => {
        if (r.status === 401) {
          if (statusPill) statusPill.style.display = 'none';
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
  setInterval(updateListenerStatus, 10 * 1000);
})();


(function(){
  const navToggle = document.getElementById('nav-toggle');
  const navMenu = document.getElementById('nav-menu');

  if (!navToggle || !navMenu) return;

  navToggle.addEventListener('click', () => {
    navMenu.classList.toggle('active');
    navToggle.classList.toggle('active');
  });

  navMenu.querySelectorAll('a').forEach(link => {
    link.addEventListener('click', () => {
      navMenu.classList.remove('active');
      navToggle.classList.remove('active');
    });
  });
})();

// Smooth fade-in for track cover images
(function() {
  document.addEventListener('load', (e) => {
    if (e.target.tagName === 'IMG' && e.target.classList.contains('track-cover')) {
      e.target.classList.add('loaded');
    }
  }, true);

  // Check already completed images (e.g. from cache) on DOMContentLoaded
  function markLoadedImages() {
    document.querySelectorAll('img.track-cover').forEach(img => {
      if (img.complete) {
        img.classList.add('loaded');
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', markLoadedImages);
  } else {
    markLoadedImages();
  }
})();

// Scroll-to-top button with circular progress. Deferred to DOMContentLoaded:
// this script runs during body parse, before the #scroll-to-top button
// (near </body>) exists, so an immediate getElementById returned null and
// the button never worked.
(function() {
  function initScrollToTop() {
  const btn = document.getElementById('scroll-to-top');
  const circle = btn ? btn.querySelector('.progress-ring__circle') : null;
  if (!btn || !circle) return;

  const radius = circle.r.baseVal.value;
  const circumference = 2 * Math.PI * radius;

  circle.style.strokeDasharray = `${circumference} ${circumference}`;
  circle.style.strokeDashoffset = circumference;

  function setProgress(percent) {
    const offset = circumference - (percent / 100) * circumference;
    circle.style.strokeDashoffset = offset;
  }

  function handleScroll() {
    const scrollTop = window.scrollY || document.documentElement.scrollTop;
    const scrollHeight = document.documentElement.scrollHeight - document.documentElement.clientHeight;

    if (scrollTop > 300) {
      btn.classList.add('visible');
      btn.style.display = 'flex';
    } else {
      btn.classList.remove('visible');
      // Delay display none slightly for fade-out animation
      setTimeout(() => {
        if (!btn.classList.contains('visible')) {
          btn.style.display = 'none';
        }
      }, 300);
    }

    if (scrollHeight > 0) {
      const percent = (scrollTop / scrollHeight) * 100;
      setProgress(percent);
    } else {
      setProgress(0);
    }
  }

  window.addEventListener('scroll', handleScroll);

  btn.addEventListener('click', () => {
    window.scrollTo({
      top: 0,
      behavior: 'smooth'
    });
  });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initScrollToTop);
  } else {
    initScrollToTop();
  }
})();
