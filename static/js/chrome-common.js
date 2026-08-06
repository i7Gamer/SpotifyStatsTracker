// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

// Chrome shared by BOTH layouts (layout.html and layout_public.html): the
// track-cover fade-in and the scroll-to-top progress ring. These lived as
// byte-identical copies in layout-chrome.js and layout-public-chrome.js
// (78 of the public file's 85 lines) until the copies were folded here -
// the public file was nothing else, so it's gone; the authenticated-only
// pieces (version badge, listener pill, nav toggle, search helpers) stay
// in layout-chrome.js.

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
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initScrollToTop);
  } else {
    initScrollToTop();
  }
})();
