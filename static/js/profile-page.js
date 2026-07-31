// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

/* Profile page AJAX tab switcher.
 *
 * Clicking a .profile-subnav link fetches the target page, extracts the
 * section content and the updated sub-nav HTML from the response, swaps them
 * into the current DOM, and updates the browser URL via pushState.  The page
 * header (identity block) and the log-out row never change between tabs so
 * they are left untouched.
 *
 * Graceful degradation: if fetch fails, or the user has JS disabled, the
 * ordinary full-page navigation takes over silently.
 *
 * Exported on window AND as module.exports so the module can be unit-tested
 * under node. */

(function () {
  'use strict';

  /* ------------------------------------------------------------------ */
  /* Selectors - kept as constants so tests can verify they match the    */
  /* markup produced by _profile_base.html.                              */
  /* ------------------------------------------------------------------ */
  var SUBNAV_SEL      = '.profile-subnav';
  var LOADING_CLASS   = 'profile-tab-loading';

  /* ------------------------------------------------------------------ */
  /* Parsing helpers                                                     */
  /* ------------------------------------------------------------------ */

  /** Parse an HTML string into a DocumentFragment we can query against.
   *  Works in browsers with DOMParser; in node the caller must supply a
   *  stub (see the test file). */
  function _parse(html) {
    if (typeof DOMParser !== 'undefined') {
      return new DOMParser().parseFromString(html, 'text/html');
    }
    return null;
  }

  /** Extract only the subnav + inter-nav-logout siblings from a parsed doc. */
  function _extractNav(doc) {
    var nav = doc.querySelector(SUBNAV_SEL);
    return nav ? nav.outerHTML : null;
  }

  /** Extract tab body sections from a parsed doc as raw HTML string. */
  function _extractBody(doc) {
    var nav    = doc.querySelector(SUBNAV_SEL);
    var logout = doc.querySelector('.profile-logout-row');
    if (!nav) return null;

    var parts = [];
    var cur = nav.nextElementSibling;
    while (cur && cur !== logout) {
      parts.push(cur.outerHTML);
      cur = cur.nextElementSibling;
    }
    return parts.join('\n');
  }

  /* ------------------------------------------------------------------ */
  /* Active-tab DOM update                                               */
  /* ------------------------------------------------------------------ */

  /** Update the active class / aria-current on the subnav without a swap. */
  function _syncNav(href) {
    var nav = document.querySelector(SUBNAV_SEL);
    if (!nav) return;
    nav.querySelectorAll('a').forEach(function (a) {
      var match = a.getAttribute('href') === href;
      a.classList.toggle('active', match);
      if (match) {
        a.setAttribute('aria-current', 'page');
      } else {
        a.removeAttribute('aria-current');
      }
    });
  }

  /* ------------------------------------------------------------------ */
  /* Core swap                                                           */
  /* ------------------------------------------------------------------ */

  /** Replace the tab-body sections in the live DOM with `html`. */
  function _swapBody(html) {
    var nav    = document.querySelector(SUBNAV_SEL);
    var logout = document.querySelector('.profile-logout-row');
    if (!nav) return;

    /* Remove every sibling between subnav and logout. */
    var cur = nav.nextElementSibling;
    while (cur && cur !== logout) {
      var next = cur.nextElementSibling;
      cur.parentNode.removeChild(cur);
      cur = next;
    }

    /* Insert fresh content. */
    var tmp = document.createElement('div');
    tmp.innerHTML = html;
    var parent = nav.parentNode;
    var before = logout || null;
    var child;
    //< the extra parens are what mark this assignment as deliberate; that is
    //  exactly no-cond-assign's "except-parens" default, so no suppression is
    //  needed (and an unused one is itself a lint error - see eslint.config.js)
    while ((child = tmp.firstChild)) {
      parent.insertBefore(child, before);
    }
  }

  /** Attributes worth carrying from an inert script node onto its
   *  replacement. `type` decides whether - and how - the browser runs it at
   *  all (module vs classic vs a non-executable type), so dropping it would
   *  change the script's meaning. Nothing else in the tab partials sets one. */
  var REVIVED_SCRIPT_ATTRS = ['type'];

  /** Swap one inert <script> node for a freshly created, equivalent one.
   *
   *  An inline <script> inserted via innerHTML is inert by spec, so it has to
   *  be re-created to run. This deliberately builds a real element rather than
   *  compiling the text: the string-compiling call this replaced needed
   *  'unsafe-eval', which the baseline CSP does not grant - it is scoped to
   *  the three detail routes for Spotify's iFrame bundle (DETAIL_PAGE_CSP in
   *  config.py). On /profile the call therefore threw EvalError straight into
   *  the catch below and the Account tab's theme-selector initialiser silently
   *  never ran after an AJAX swap. A script *element* is covered by
   *  'unsafe-inline', which the baseline does grant, so this runs under the
   *  policy the page actually ships with instead of asking to widen it.
   *  tests/test_security_headers.py scans this tree for the whole eval family,
   *  so re-introducing one fails the suite rather than failing in silence. */
  function _reviveScript(stale) {
    var fresh = document.createElement('script');
    REVIVED_SCRIPT_ATTRS.forEach(function (name) {
      var value = stale.getAttribute(name);
      if (value !== null) fresh.setAttribute(name, value);
    });
    fresh.textContent = stale.textContent;
    /* replaceChild, not append-then-remove: inserting the replacement is what
       executes it, so it must land in the position the original held. */
    if (stale.parentNode) stale.parentNode.replaceChild(fresh, stale);
  }

  /** Re-execute inline <script> blocks that may be in the swapped content
   *  (e.g. the theme-selector initialiser on the Account tab). */
  function _runInlineScripts() {
    var nav = document.querySelector(SUBNAV_SEL);
    if (!nav) return;
    var logout = document.querySelector('.profile-logout-row');
    var cur = nav.nextElementSibling;
    while (cur && cur !== logout) {
      var next = cur.nextElementSibling;
      /* Snapshot before mutating: replaceChild edits the live tree the
         NodeList this returns is a view of. */
      Array.prototype.slice.call(cur.querySelectorAll('script')).forEach(function (s) {
        /* Only text-content scripts; skip src= ones (not present here) -
           re-adding one would re-download and re-run a whole file. */
        if (!s.src) {
          try {
            _reviveScript(s);
          } catch (_) { /* non-fatal */ }
        }
      });
      cur = next;
    }
  }

  /* ------------------------------------------------------------------ */
  /* Public API                                                          */
  /* ------------------------------------------------------------------ */

  var ProfilePage = {
    SUBNAV_SEL:    SUBNAV_SEL,
    LOADING_CLASS: LOADING_CLASS,

    /** Fetch `url` and swap in its tab body. Returns a Promise. */
    navigate: function (url) {
      var card = document.querySelector('.login-card');
      if (card) card.classList.add(LOADING_CLASS);

      return fetch(url, { credentials: 'same-origin' })
        .then(function (resp) {
          if (!resp.ok) throw new Error('HTTP ' + resp.status);
          return resp.text();
        })
        .then(function (html) {
          var doc = _parse(html);
          if (!doc) throw new Error('DOMParser unavailable');

          var bodyHtml = _extractBody(doc);
          if (bodyHtml === null) throw new Error('body not found in response');

          _swapBody(bodyHtml);
          _syncNav(url.replace(location.origin, ''));
          _runInlineScripts();
          return true;
        })
        .finally(function () {
          if (card) card.classList.remove(LOADING_CLASS);
        });
    },

    /** Wire up subnav link interception and popstate handling.
     *  Idempotent - safe to call more than once. */
    init: function () {
      var nav = document.querySelector(SUBNAV_SEL);
      if (!nav || nav._profileAjaxWired) return;
      nav._profileAjaxWired = true;

      nav.addEventListener('click', function (e) {
        var link = e.target.closest('a[href]');
        if (!link) return;

        var href = link.getAttribute('href');
        /* Only intercept same-origin profile paths. */
        if (!href || href.charAt(0) !== '/') return;
        if (!href.startsWith('/profile')) return;

        e.preventDefault();

        /* Push state before navigating so popstate can get the URL. */
        history.pushState({ profileTab: href }, '', href);

        ProfilePage.navigate(location.href).catch(function () {
          /* Graceful degradation: hard-navigate on error. */
          location.href = href;
        });
      });

      window.addEventListener('popstate', function (e) {
        if (e.state && e.state.profileTab) {
          ProfilePage.navigate(location.href).catch(function () {
            location.reload();
          });
        }
      });
    },

    /* Exposed for testing. */
    _parse:             _parse,
    _extractNav:        _extractNav,
    _extractBody:       _extractBody,
    _swapBody:          _swapBody,
    _syncNav:           _syncNav,
    _runInlineScripts:  _runInlineScripts,
  };

  /* Make available globally and as a CJS module (node tests). */
  if (typeof window !== 'undefined') window.ProfilePage = ProfilePage;
  if (typeof module !== 'undefined') module.exports = ProfilePage;
}());
