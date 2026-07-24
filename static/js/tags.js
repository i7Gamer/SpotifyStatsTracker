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
      var html = '<span style="font-size: 0.9rem; font-weight: bold; color: var(--text-muted, #888888);">Tags:</span>';
      if (tags && tags.length > 0) {
        tags.forEach(function(t) {
          html += '<span class="tag-chip" data-tag="' + t + '" style="display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 12px; background: rgba(251, 113, 123, 0.15); color: var(--accent-color, #fb717b); font-size: 0.85rem;">' +
                  '#' + t +
                  '<button type="button" class="btn-remove-tag" data-tag="' + t + '" style="background: none; border: none; color: inherit; cursor: pointer; padding: 0 2px; font-weight: bold;">&times;</button>' +
                  '</span>';
        });
      } else {
        html += '<span class="no-tags-text" style="font-size: 0.85rem; color: var(--text-muted, #888888); italic;">No tags added yet</span>';
      }
      chipsContainer.innerHTML = html;
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
