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

  function flash(button, text) {
    if (button.dataset.restoreText === undefined) {
      button.dataset.restoreText = button.textContent;
    }
    button.textContent = text;
    clearTimeout(button._copyTimer);
    button._copyTimer = setTimeout(function () {
      button.textContent = button.dataset.restoreText;
      delete button.dataset.restoreText;
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
    module.exports = { copyFeedbackText: copyFeedbackText, COPIED_TEXT: COPIED_TEXT, FAILED_TEXT: FAILED_TEXT };
  }
})();
