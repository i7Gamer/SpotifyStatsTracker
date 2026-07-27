// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

(function() {
  document.addEventListener('DOMContentLoaded', function() {
    var widget = document.querySelector('.tag-widget');
    if (!widget) return;

    var entityType = widget.dataset.entityType;
    var entityId = widget.dataset.entityId;
    var chipsContainer = widget.querySelector('.tag-chips-container');
    var addForm = widget.querySelector('.form-add-tag');
    var tagInput = widget.querySelector('.input-new-tag');

    function getCsrfToken() {
      var meta = document.querySelector('meta[name="csrf-token"]');
      if (meta) return meta.getAttribute('content');
      var input = document.querySelector('input[name="csrf_token"]');
      return input ? input.value : '';
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
        empty.className = 'no-tags-text';
        empty.style.cssText = 'font-size: 0.85rem; color: var(--text-muted, #888888); italic;';
        empty.textContent = 'No tags added yet';
        chipsContainer.appendChild(empty);
      }
      bindRemoveButtons();
    }

    function bindRemoveButtons() {
      var removeBtns = chipsContainer.querySelectorAll('.btn-remove-tag');
      removeBtns.forEach(function(btn) {
        btn.addEventListener('click', function() {
          var tagToRemove = btn.dataset.tag;
          fetch('/api/tags', {
            method: 'DELETE',
            headers: {
              'Content-Type': 'application/json',
              'X-CSRFToken': getCsrfToken()
            },
            body: JSON.stringify({
              entity_type: entityType,
              entity_id: entityId,
              tag: tagToRemove
            })
          })
          .then(function(res) { return res.json(); })
          .then(function(data) {
            if (data.success) {
              renderTags(data.tags);
            }
          })
          .catch(function(err) {
            console.error('Error removing tag:', err);
          });
        });
      });
    }

    if (addForm) {
      addForm.addEventListener('submit', function(evt) {
        evt.preventDefault();
        var tagVal = tagInput.value.trim();
        if (!tagVal) return;

        fetch('/api/tags', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCsrfToken()
          },
          body: JSON.stringify({
            entity_type: entityType,
            entity_id: entityId,
            tag: tagVal
          })
        })
        .then(function(res) { return res.json(); })
        .then(function(data) {
          if (data.success) {
            tagInput.value = '';
            renderTags(data.tags);
          }
        })
        .catch(function(err) {
          console.error('Error adding tag:', err);
        });
      });
    }

    bindRemoveButtons();
  });
})();
