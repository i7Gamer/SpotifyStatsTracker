// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

/* The inline tag widget on the song/artist/album detail pages
 * (templates/_tag_widget.html): renders the chip row and drives the
 * add/remove calls against /api/tags. */

// --- pure helper ------------------------------------------------------------
// Module scope, DOM-free and exported (the top-list.js pattern), so the
// response-handling branches are unit-testable in plain node.

// Interprets one settled /api/tags response. `ok` is res.ok; `data` is the
// parsed JSON body, or null when parsing failed (the admin kill switch
// answers with an aborted 404's HTML page, not JSON). The old handlers did
// `if (data.success) { ... }` with no else, so every failure - a 401 after
// the session expired, a 400 on a tag rejected server-side, a 500 - left
// the widget looking untouched, with the typed text still in the input and
// nothing logged or shown.
function tagUpdateOutcome(ok, data, fallbackMessage) {
  if (ok && data && data.success) {
    return { apply: true, tags: data.tags || [] };
  }
  //< the API's 400s carry messages worth showing verbatim ("Tag is empty
  //  after normalization", a length-limit ValueError); only a body with no
  //  usable message gets the generic line
  return { apply: false, message: (data && data.error) || fallbackMessage };
}

if (typeof window !== 'undefined') (function() {
  document.addEventListener('DOMContentLoaded', function() {
    var widget = document.querySelector('.tag-widget');
    if (!widget) return;

    var entityType = widget.dataset.entityType;
    var entityId = widget.dataset.entityId;
    var chipsContainer = widget.querySelector('.tag-chips-container');
    var addForm = widget.querySelector('.form-add-tag');
    var tagInput = widget.querySelector('.input-new-tag');
    var errorLine = widget.querySelector('.tag-error');

    //< the hidden input Flask-WTF renders. There used to be a
    //  meta[name="csrf-token"] branch ahead of this one, which no template has
    //  ever emitted - so it was dead on every path, and read like a second
    //  supported convention that a reader had to check for.
    function getCsrfToken() {
      var input = document.querySelector('input[name="csrf_token"]');
      return input ? input.value : '';
    }

    function showTagError(message) {
      if (!errorLine) return;
      errorLine.textContent = message;
      errorLine.style.display = 'block';
    }

    function hideTagError() {
      if (!errorLine) return;
      errorLine.textContent = '';
      errorLine.style.display = 'none';
    }

    //< which submit is the latest. Nothing disables the form or the chips for
    //  the round trip, so two updates can be in flight at once, and each
    //  answer carries the server's full list AS OF that request: the one that
    //  lands last used to paint the row - an older list over a newer one, so a
    //  tag the server had stored vanished until reload. Same guard as
    //  wrapped.js's shareSubmitSeq and playlists.js's previewToken.
    var tagSeq = 0;

    //< In-flight count, whether more than one request has ever overlapped in
    //  the current span, and the most recently issued request's onApplied.
    //  The server can commit+read the SECOND of two concurrent writes before
    //  the FIRST's commit lands, so a response's OWN tags list can be wrong
    //  even when tagSeq correctly says it's the latest one issued - no amount
    //  of client sequencing fixes a body that is itself incomplete. tagSeq
    //  still decides whether a given response's error (or, outside any
    //  overlap, its success) is acted on; this decides whether a *success* may
    //  be trusted at all. Reset together once the span drains to zero.
    var inFlightTagRequests = 0;
    var tagRequestsOverlapped = false;
    var latestOnApplied = null;

    // One authoritative read of this entity's current tags, from the page
    // itself rather than a dedicated JSON endpoint - routes/tags.py exposes
    // no per-entity GET, only /api/tags's all-tags summary (for the
    // Playlists page) and whatever an add/remove echoes back. The detail
    // page this widget lives on already renders _tag_widget.html from
    // getTagsForEntity at request time (routes/charts.py), so re-fetching the
    // current URL and reading its .tag-widget back out is the same data a
    // fresh load would show, without a full navigation.
    function refetchAuthoritativeTags(onApplied) {
      fetch(window.location.href, { credentials: 'same-origin' })
        .then(function(res) {
          if (window.AjaxStatus && window.AjaxStatus.redirectIfUnauthorized(res)) {
            return null;
          }
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.text();
        })
        .then(function(html) {
          if (html === null) return;   //< navigating to /login
          if (typeof DOMParser === 'undefined') throw new Error('DOMParser unavailable');
          var freshWidget = new DOMParser().parseFromString(html, 'text/html').querySelector('.tag-widget');
          if (!freshWidget) throw new Error('tag widget not found in refetched page');
          var tags = Array.prototype.map.call(
            freshWidget.querySelectorAll('.tag-chip'),
            function(chip) { return chip.dataset.tag; });
          onApplied(tags);
        })
        .catch(function(err) {
          console.error('Tag list refresh failed:', err);
          showTagError("Couldn't refresh the tag list. Please reload the page.");
        });
    }

    // One request path for add and remove - they differ only in HTTP method,
    // the tag sent, and what happens after a successful apply.
    function submitTagUpdate(method, tag, fallbackMessage, onApplied) {
      var seq = ++tagSeq;
      inFlightTagRequests++;
      if (inFlightTagRequests > 1) tagRequestsOverlapped = true;
      latestOnApplied = onApplied;

      //< Always runs last, whichever branch below took: drops the in-flight
      //  count and, once it reaches zero, fires the one refetch a span that
      //  ever overlapped owes.
      function settleTagRequest() {
        inFlightTagRequests--;
        if (inFlightTagRequests === 0 && tagRequestsOverlapped) {
          tagRequestsOverlapped = false;
          refetchAuthoritativeTags(latestOnApplied);
        }
      }

      fetch('/api/tags', {
        method: method,
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': getCsrfToken()
        },
        body: JSON.stringify({
          entity_type: entityType,
          entity_id: entityId,
          tag: tag
        })
      })
      .then(function(res) {
        //< an expired session gets the shared login redirect (coming back to
        //  this page), not an inline "unauthorized"
        if (window.AjaxStatus && window.AjaxStatus.redirectIfUnauthorized(res)) {
          return null;
        }
        return res.json()
          .catch(function() { return null; })
          .then(function(data) { return tagUpdateOutcome(res.ok, data, fallbackMessage); });
      })
      .then(function(outcome) {
        if (!outcome) { settleTagRequest(); return; }   //< navigating to /login
        /* Checked AFTER the 401 peel above and never before it: a 401 is news
           about the SESSION, which every in-flight submit shares, so whichever
           one notices acts on it (see wrapped.js, same rule). */
        if (seq !== tagSeq) { settleTagRequest(); return; }
        if (outcome.apply) {
          hideTagError();
          //< mid-span, this response's own tags list isn't trusted - only the
          //  refetch settleTagRequest schedules once the span drains may
          //  repaint (see the field comments above)
          if (!tagRequestsOverlapped) onApplied(outcome.tags);
        } else {
          showTagError(outcome.message);
        }
        settleTagRequest();
      })
      .catch(function(err) {
        console.error('Tag update failed:', err);
        //< gated too: an error about a request the user has already moved past
        //  is as wrong as a stale repaint
        if (seq === tagSeq) showTagError(fallbackMessage);
        settleTagRequest();
      });
    }

    function renderTags(tags) {
      chipsContainer.innerHTML = '';

      var label = document.createElement('span');
      label.style.cssText = 'font-size: 0.9rem; font-weight: bold; color: var(--muted);';
      label.textContent = 'Tags:';
      chipsContainer.appendChild(label);

      if (tags && tags.length > 0) {
        tags.forEach(function(t) {
          var chip = document.createElement('span');
          //< styling lives on the classes now (see .tag-chip/.btn-remove-tag
          //  in style.css) - this render and _tag_widget.html used to carry
          //  duplicate cssText/style copies that could drift apart
          chip.className = 'tag-chip';
          chip.dataset.tag = t;
          chip.appendChild(document.createTextNode('#' + t));

          var removeBtn = document.createElement('button');
          removeBtn.type = 'button';
          removeBtn.className = 'btn-remove-tag';
          removeBtn.setAttribute('aria-label', 'Remove tag ' + t);
          removeBtn.title = 'Remove tag';
          removeBtn.dataset.tag = t;
          removeBtn.innerHTML = '&times;';
          chip.appendChild(removeBtn);

          chipsContainer.appendChild(chip);
        });
      } else {
        var empty = document.createElement('span');
        empty.className = 'no-tags-text';   //< styled in style.css, like the template's copy
        empty.textContent = 'No tags added yet';
        chipsContainer.appendChild(empty);
      }
      bindRemoveButtons();
    }

    function bindRemoveButtons() {
      var removeBtns = chipsContainer.querySelectorAll('.btn-remove-tag');
      removeBtns.forEach(function(btn) {
        btn.addEventListener('click', function() {
          submitTagUpdate('DELETE', btn.dataset.tag,
            "Couldn't remove that tag. Please try again.", function(tags) {
              renderTags(tags);
              //< the button that was clicked is gone once the row repaints,
              //  and the browser's default is to drop focus to <body>
              if (tagInput) tagInput.focus();
            });
        });
      });
    }

    if (addForm) {
      addForm.addEventListener('submit', function(evt) {
        evt.preventDefault();
        var tagVal = tagInput.value.trim();
        if (!tagVal) return;

        submitTagUpdate('POST', tagVal,
          "Couldn't add that tag. Please try again.", function(tags) {
            tagInput.value = '';
            renderTags(tags);
          });
      });
    }

    bindRemoveButtons();
  });
})();

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { tagUpdateOutcome };
}
