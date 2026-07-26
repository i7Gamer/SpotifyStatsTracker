// The public (share-link) pages' chrome. Same story as layout-chrome.js:
// extracted verbatim from layout_public.html so it can be linted; no Jinja,
// same position, same execution order.
// Smooth fade-in for track cover images - mirrors layout.html.
(function() {
  document.addEventListener('load', (e) => {
    if (e.target.tagName === 'IMG' && e.target.classList.contains('track-cover')) {
      e.target.classList.add('loaded');
    }
  }, true);

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

// Scroll-to-top button with circular progress - mirrors layout.html.
// Deferred to DOMContentLoaded because the button element lives near
// </body>, after this parse-time script.
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
