// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

// Chrome shared by BOTH layouts (layout.html and layout_public.html): the
// track-cover fade-in and the scroll-to-top progress ring. These lived as
// byte-identical copies in layout-chrome.js and layout-public-chrome.js
// (78 of the public file's 85 lines) until the copies were folded here -
// the public file was nothing else, so it's gone; the authenticated-only
// pieces (version badge, listener pill, nav toggle, search helpers) stay
// in layout-chrome.js.

// The theme, kept in step across a user's open tabs.
//
// It is chosen on /profile, which writes the class onto <html> and the value
// into localStorage; the <head> bootstrap in both layouts reads it back at the
// next page load. Every OTHER tab already open kept the old theme until it
// happened to reload - and the canvas charts are worse off than the CSS, since
// they read their colours from the variables once, at paint time, and hold
// them until something repaints.
//
// charts.js and genres.js each carried a listener for that repaint, bound to
// '#theme-selector'. That element exists only on /profile, a page with no
// charts, so neither had ever fired. A storage event is what actually crosses
// tabs. Applying the theme is the layout's job, so it happens here; the chart
// files only need to know it changed, which is what THEME_CHANGE_EVENT says.
const THEME_STORAGE_KEY = 'spotify-stats-theme';
const THEME_DEFAULT_CLASS = 'theme-rose';   //< must match both layouts' <head> bootstrap
const THEME_CHANGE_EVENT = 'themechange';

(function() {
  window.addEventListener('storage', (event) => {
    if (!event || event.key !== THEME_STORAGE_KEY) return;
    //< null is what a localStorage.clear() in the other tab delivers
    const theme = event.newValue || THEME_DEFAULT_CLASS;
    //< a storage event fires for a write that changed nothing, and announcing
    //  that would cost every chart on the page a full repaint for no reason
    if (document.documentElement.className === theme) return;
    document.documentElement.className = theme;
    window.dispatchEvent(new Event(THEME_CHANGE_EVENT));
  });
})();

// Smooth fade-in for track cover images. The capture-phase listener catches
// `load` on any img.track-cover including ones swapped in later by AJAX.
(function() {
  document.addEventListener('load', (e) => {
    if (e.target.tagName === 'IMG' && e.target.classList.contains('track-cover')) {
      e.target.classList.add('loaded');
    }
  }, true);

  // Check already completed images (e.g. from cache): those never fire `load`,
  // so the capture listener above never sees them.
  function markLoadedImages(root) {
    (root || document).querySelectorAll('img.track-cover').forEach(img => {
      if (img.complete) {
        img.classList.add('loaded');
      }
    });
  }

  // Wrapped, not passed straight in: a listener is handed the Event, and this
  // takes a root ELEMENT - so `markLoadedImages` as the handler swept
  // `(Event || document)` and threw on every page load, taking the sweep with
  // it. The sibling registration below reads the root off the event on purpose.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => markLoadedImages());
  } else {
    markLoadedImages();
  }

  // The same sweep after every htmx swap, for the same reason: covers arriving
  // in swapped-in markup are frequently cache hits, already `complete` before
  // any listener could attach.
  //
  // This lives here rather than per page, and that is the point. The sweep was
  // already this file's job - it just only ran once, at DOMContentLoaded - so
  // five migrated pages had each grown their own copy inside their own
  // htmx:afterSwap handler, re-solving a solved problem because nothing
  // re-invoked the solution. On `document`, not document.body, so it does not
  // depend on where in the page this script is loaded.
  document.addEventListener('htmx:afterSwap', evt => markLoadedImages(evt.target));
})();

// Scroll-to-top button with circular progress. Init is deferred to
// DOMContentLoaded: this script runs during body parse, before the
// #scroll-to-top button (near </body>) exists, so an immediate
// getElementById returned null and the button never worked.
//
// TWO thresholds, not one. A single `scrollTop > 300` put the button on a knife
// edge for any page whose whole scrollable distance is near 300 - and this app
// changes page height AFTER you have scrolled, constantly: a lazy cover
// settling, a chart taking its real size, a deferred body swap. The browser
// clamps scrollY to the shorter page, that lands a few pixels under the line,
// and the button hid itself while the reader sat still. Measured in a browser:
// shown at y=322, gone 300ms later at y=292.
//
// The gap between them is the wobble a height change is allowed to cause. Going
// UP still hides it at the lower line, which is what a reader actually does.
const SCROLL_TOP_SHOW_AT_PX = 300;
const SCROLL_TOP_HIDE_AT_PX = 200;

function scrollTopButtonVisible(scrollTop, wasVisible) {
  return scrollTop > (wasVisible ? SCROLL_TOP_HIDE_AT_PX : SCROLL_TOP_SHOW_AT_PX);
}

(function() {
  function initScrollToTop() {
  const btn = document.getElementById('scroll-to-top');
  const circle = btn ? btn.querySelector('.progress-ring__circle') : null;
  if (!btn || !circle) return;

  const radius = circle.r.baseVal.value;
  const circumference = 2 * Math.PI * radius;
  let hideTimer = null;

  circle.style.strokeDasharray = `${circumference} ${circumference}`;
  circle.style.strokeDashoffset = circumference;

  function setProgress(percent) {
    const offset = circumference - (percent / 100) * circumference;
    circle.style.strokeDashoffset = offset;
  }

  function update() {
    const scrollTop = window.scrollY || document.documentElement.scrollTop;
    const scrollHeight = document.documentElement.scrollHeight - document.documentElement.clientHeight;

    if (scrollTopButtonVisible(scrollTop, btn.classList.contains('visible'))) {
      //< a pending hide from a moment ago must not land on a button that is
      //  visible again; the class check inside it caught this, cancelling says it
      clearTimeout(hideTimer);
      hideTimer = null;
      btn.classList.add('visible');
      btn.style.display = 'flex';
    } else {
      btn.classList.remove('visible');
      // Delay display none slightly for fade-out animation
      clearTimeout(hideTimer);
      hideTimer = setTimeout(() => {
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

  window.addEventListener('scroll', update);
  //< the button's state is a function of the page as well as the scroll, and
  //  both of the other two move without a scroll event: a resized window, and
  //  the deferred body every two-phase page swaps in after first paint
  window.addEventListener('resize', update);
  document.addEventListener('htmx:afterSwap', update);

  btn.addEventListener('click', () => {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });

  //< once up front: a reload or a Back restores the scroll position WITHOUT
  //  firing a scroll event, so the button stayed hidden on an already-scrolled
  //  page until the reader happened to move
  update();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initScrollToTop);
  } else {
    initScrollToTop();
  }
})();

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { scrollTopButtonVisible };
}

// Playlist-file download buttons (_playlist_download.html): ONE delegated
// handler for every page the shared control appears on - Wrapped's Top 100
// (whose bespoke wrapped.js branch this replaced) and the Compare blend.
// It reads the format <select> the button names and NAVIGATES to the export
// URL with the format appended, so the browser's own download flow handles
// the attachment; appending with ? or & lets the URL arrive with or without
// a query of its own. Delegated off document because Compare's control
// arrives inside an htmx swap - a listener bound at load would never see it.
(function() {
  document.addEventListener('click', (evt) => {
    const btn = evt.target.closest('.js-playlist-download');
    if (!btn) return;
    const select = document.getElementById(btn.dataset.formatSelect);
    const format = select ? select.value : 'csv';
    const url = btn.dataset.exportUrl;
    const joiner = url.indexOf('?') === -1 ? '?' : '&';
    window.location.href = url + joiner + 'format=' + encodeURIComponent(format);
  });
})();
