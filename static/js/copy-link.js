// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

/* Shared "copy link" button handler (profile share links, Wrapped share panel).
 * Copies the button's data-url to the clipboard with graceful fallbacks and
 * visible feedback. The old inline onclick called navigator.clipboard.writeText
 * directly, which is undefined on a non-secure origin (e.g. a self-hosted
 * instance reached over a plain-HTTP LAN IP) - there it threw and copied
 * nothing, with no indication to the user. This tries the async Clipboard API,
 * falls back to a hidden-textarea execCommand copy, and always flashes a
 * "Copied!" / "Copy failed" confirmation on the button. */
(function () {
  var COPIED_TEXT = 'Copied!';
  var FAILED_TEXT = 'Copy failed';
  var RESTORE_MS = 1500;
  //< marks the hidden live region beside a Copy button (see liveRegionFor)
  var LIVE_REGION_ATTR = 'data-copy-status';
  var LIVE_REGION_CLASS = 'visually-hidden';

  function copyFeedbackText(success) {
    return success ? COPIED_TEXT : FAILED_TEXT;
  }

  function fallbackCopy(text) {
    // execCommand path for insecure contexts / older browsers without the
    // async Clipboard API. Returns whether the copy succeeded.
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'absolute';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  /* Where the flash is ANNOUNCED, as opposed to where it is seen.
     role="status" used to go on the button itself. That has two problems: the
     attribute was never removed, so from the first click the control stayed
     exposed as a status region rather than a button for the life of the page;
     and a node that becomes a live region and receives its content in the same
     tick is ignored by several screen readers, so the announcement it was added
     for was unreliable anyway.
     A live region has to EXIST before the text it carries changes, so
     _share_link_panel.html renders one hidden span per Copy button. One is
     created here as a fallback for any caller that does not, and remembered on
     the button so a second click reuses it rather than stacking spans. */
  function liveRegionFor(button) {
    if (button._copyStatusRegion !== undefined) return button._copyStatusRegion;

    var region = null;
    var sibling = button.nextElementSibling;
    if (sibling && sibling.hasAttribute && sibling.hasAttribute(LIVE_REGION_ATTR)) {
      region = sibling;
    } else if (typeof document !== 'undefined' && button.parentNode) {
      region = document.createElement('span');
      region.className = LIVE_REGION_CLASS;
      region.setAttribute(LIVE_REGION_ATTR, '');
      region.setAttribute('role', 'status');
      region.setAttribute('aria-live', 'polite');
      button.parentNode.insertBefore(region, button.nextSibling);
    }
    button._copyStatusRegion = region;
    return region;
  }

  function flash(button, text) {
    if (button.dataset.restoreText === undefined) {
      button.dataset.restoreText = button.textContent;
    }
    //< the button's own label is the visible half; the region is the audible one
    var region = liveRegionFor(button);
    if (region) region.textContent = text;
    button.textContent = text;
    clearTimeout(button._copyTimer);
    button._copyTimer = setTimeout(function () {
      button.textContent = button.dataset.restoreText;
      delete button.dataset.restoreText;
      //< the REGION stays (it must outlive the message it announced); only its
      //  text goes, so the next copy is announced as a change rather than a
      //  repeat of the same string
      if (region) region.textContent = '';
    }, RESTORE_MS);
  }

  function copyShareLink(button) {
    var url = button && button.dataset ? button.dataset.url : '';
    if (!url) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(
        function () { flash(button, COPIED_TEXT); },
        function () { flash(button, copyFeedbackText(fallbackCopy(url))); }
      );
    } else {
      flash(button, copyFeedbackText(fallbackCopy(url)));
    }
  }

  if (typeof window !== 'undefined') {
    window.copyShareLink = copyShareLink;
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      copyFeedbackText: copyFeedbackText, flash: flash,
      COPIED_TEXT: COPIED_TEXT, FAILED_TEXT: FAILED_TEXT,
      LIVE_REGION_ATTR: LIVE_REGION_ATTR, RESTORE_MS: RESTORE_MS,
    };
  }
})();
