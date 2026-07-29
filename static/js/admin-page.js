// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

/* Admin page AJAX tab switcher.
 *
 * Clicking an .admin-subnav link fetches the target page, extracts the
 * #admin-tab-body section content from the response, swaps it into the current
 * DOM, and updates the browser URL via pushState. Registered Users & Sync Status
 * remains permanently pinned above the tabs.
 *
 * Graceful degradation: if fetch fails, or JS is disabled, full-page
 * navigation takes over silently. */

(function () {
  'use strict';

  var SUBNAV_SEL    = '.admin-subnav';
  var BODY_ID       = 'admin-tab-body';
  var LOADING_CLASS = 'admin-tab-loading';

  function _parse(html) {
    if (typeof DOMParser !== 'undefined') {
      return new DOMParser().parseFromString(html, 'text/html');
    }
    return null;
  }

  function _extractBody(doc) {
    var body = doc.getElementById(BODY_ID);
    return body ? body.innerHTML : null;
  }

  function _syncNav(href) {
    var nav = document.querySelector(SUBNAV_SEL);
    if (!nav) return;
    var targetUrl = new URL(href, location.origin);
    var targetTab = targetUrl.searchParams.get('tab') || 'overview';

    nav.querySelectorAll('a').forEach(function (a) {
      var linkUrl = new URL(a.getAttribute('href'), location.origin);
      var linkTab = linkUrl.searchParams.get('tab') || 'overview';
      var match = (linkTab === targetTab);
      a.classList.toggle('active', match);
      if (match) {
        a.setAttribute('aria-current', 'page');
      } else {
        a.removeAttribute('aria-current');
      }
    });
  }

  function _swapBody(html) {
    var container = document.getElementById(BODY_ID);
    if (!container) return;
    container.innerHTML = html;
  }

  function _reviveScript(stale) {
    var fresh = document.createElement('script');
    if (stale.getAttribute('type') !== null) {
      fresh.setAttribute('type', stale.getAttribute('type'));
    }
    fresh.textContent = stale.textContent;
    if (stale.parentNode) stale.parentNode.replaceChild(fresh, stale);
  }

  function _runInlineScripts() {
    var container = document.getElementById(BODY_ID);
    if (!container) return;

    Array.prototype.slice.call(container.querySelectorAll('script')).forEach(function (s) {
      if (!s.src) {
        try {
          _reviveScript(s);
        } catch (_) { /* non-fatal */ }
      }
    });

    if (typeof window.onSkipModeChange === 'function') {
      try { window.onSkipModeChange(); } catch (_) {}
    }
  }

  var AdminPage = {
    SUBNAV_SEL: SUBNAV_SEL,
    BODY_ID: BODY_ID,

    navigate: function (url) {
      var bodyElem = document.getElementById(BODY_ID);
      if (bodyElem) bodyElem.classList.add(LOADING_CLASS);

      return fetch(url, { credentials: 'same-origin' })
        .then(function (resp) {
          if (!resp.ok) throw new Error('HTTP ' + resp.status);
          return resp.text();
        })
        .then(function (html) {
          var doc = _parse(html);
          if (!doc) throw new Error('DOMParser unavailable');

          var bodyHtml = _extractBody(doc);
          if (bodyHtml === null) throw new Error('tab body not found in response');

          _swapBody(bodyHtml);
          _syncNav(url);
          _runInlineScripts();
          return true;
        })
        .finally(function () {
          if (bodyElem) bodyElem.classList.remove(LOADING_CLASS);
        });
    },

    init: function () {
      var nav = document.querySelector(SUBNAV_SEL);
      if (!nav || nav._adminAjaxWired) return;
      nav._adminAjaxWired = true;

      nav.addEventListener('click', function (e) {
        var link = e.target.closest('a[href]');
        if (!link) return;

        var href = link.getAttribute('href');
        if (!href || href.charAt(0) !== '/') return;
        if (!href.startsWith('/admin')) return;

        e.preventDefault();

        history.pushState({ adminTab: href }, '', href);

        AdminPage.navigate(location.href).catch(function () {
          location.href = href;
        });
      });

      window.addEventListener('popstate', function (e) {
        if (e.state && e.state.adminTab) {
          AdminPage.navigate(location.href).catch(function () {
            location.reload();
          });
        }
      });
    },

    _parse: _parse,
    _extractBody: _extractBody,
    _swapBody: _swapBody,
    _syncNav: _syncNav,
    _runInlineScripts: _runInlineScripts
  };

  if (typeof window !== 'undefined') window.AdminPage = AdminPage;
  if (typeof module !== 'undefined') module.exports = AdminPage;
}());
