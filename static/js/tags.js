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

    // One request path for add and remove - they differ only in HTTP method,
    // the tag sent, and what happens after a successful apply.
    function submitTagUpdate(method, tag, fallbackMessage, onApplied) {
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
        if (!outcome) return;   //< navigating to /login
        if (outcome.apply) {
          hideTagError();
          onApplied(outcome.tags);
        } else {
          showTagError(outcome.message);
        }
      })
      .catch(function(err) {
        console.error('Tag update failed:', err);
        showTagError(fallbackMessage);
      });
    }

    function renderTags(tags) {
      chipsContainer.innerHTML = '';

      var label = document.createElement('span');
      label.style.cssText = 'font-size: 0.9rem; font-weight: bold; color: var(--text-muted, #888888);';
      label.textContent = 'Tags:';
      chipsContainer.appendChild(label);

      if (tags && tags.length > 0) {
        tags.forEach(function(t) {
          var chip = document.createElement('span');
          chip.className = 'tag-chip';
          chip.dataset.tag = t;
          chip.style.cssText = 'display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 12px; background: color-mix(in srgb, var(--accent) 15%, transparent); color: var(--accent); font-size: 0.85rem;';
          chip.appendChild(document.createTextNode('#' + t));

          var removeBtn = document.createElement('button');
          removeBtn.type = 'button';
          removeBtn.className = 'btn-remove-tag';
          removeBtn.setAttribute('aria-label', 'Remove tag ' + t);
          removeBtn.title = 'Remove tag';
          removeBtn.dataset.tag = t;
          removeBtn.style.cssText = 'background: none; border: none; color: inherit; cursor: pointer; padding: 0 2px; font-weight: bold;';
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
            "Couldn't remove that tag. Please try again.", renderTags);
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
