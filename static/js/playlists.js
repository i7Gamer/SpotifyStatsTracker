// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

(function() {
  document.addEventListener('DOMContentLoaded', function() {
    var tagChips = document.querySelectorAll('.playlist-tag-chip');
    var matchMode = document.getElementById('matchMode');
    var sortBy = document.getElementById('sortBy');
    var exportFormat = document.getElementById('exportFormat');
    var previewCount = document.getElementById('previewCount');
    var btnDownload = document.getElementById('btnDownloadPlaylist');

    var selectedTags = new Set();

    function getCsrfToken() {
      var meta = document.querySelector('meta[name="csrf-token"]');
      if (meta) return meta.getAttribute('content');
      var input = document.querySelector('input[name="csrf_token"]');
      return input ? input.value : '';
    }

    tagChips.forEach(function(chip) {
      chip.addEventListener('click', function() {
        var tag = chip.dataset.tag;
        if (selectedTags.has(tag)) {
          selectedTags.delete(tag);
          chip.style.background = 'rgba(255,255,255,0.05)';
          chip.style.borderColor = 'var(--border-color, #444)';
          chip.style.color = 'inherit';
        } else {
          selectedTags.add(tag);
          chip.style.background = 'color-mix(in srgb, var(--accent) 20%, transparent)';
          chip.style.borderColor = 'var(--accent)';
          chip.style.color = 'var(--accent)';
        }
        updatePreview();
      });
    });

    if (matchMode) matchMode.addEventListener('change', updatePreview);
    if (sortBy) sortBy.addEventListener('change', updatePreview);

    //< every preview request takes the next token; only the newest one may
    //  write the count. Without this, two quick tag toggles could resolve out
    //  of order and leave the count (and the Download button's enabled state)
    //  describing a selection the user has already moved past - including
    //  re-enabling Download after the selection was cleared to nothing.
    var previewToken = 0;

    function updatePreview() {
      var token = ++previewToken;
      if (selectedTags.size === 0) {
        if (previewCount) previewCount.textContent = '0 tracks match selection';
        if (btnDownload) btnDownload.disabled = true;
        return;
      }

      var tagsArr = Array.from(selectedTags);
      var url = '/api/playlists/preview?tags=' + encodeURIComponent(tagsArr.join(',')) +
                '&match=' + encodeURIComponent(matchMode.value);

      fetch(url)
        .then(function(res) { return res.json(); })
        .then(function(data) {
          if (token !== previewToken) return;   //< superseded by a newer selection
          var cnt = data.track_count || 0;
          if (previewCount) {
            previewCount.textContent = cnt + ' track' + (cnt !== 1 ? 's' : '') + ' match selection';
          }
          if (btnDownload) {
            btnDownload.disabled = (cnt === 0);
          }
        })
        .catch(function(err) {
          console.error('Error fetching preview count:', err);
          if (token !== previewToken) return;
          if (previewCount) previewCount.textContent = "Couldn't check how many tracks match.";
        });
    }

    if (btnDownload) {
      btnDownload.addEventListener('click', function() {
        if (selectedTags.size === 0) return;
        var tagsArr = Array.from(selectedTags);
        var exportUrl = '/playlist/export?tags=' + encodeURIComponent(tagsArr.join(',')) +
                        '&match=' + encodeURIComponent(matchMode.value) +
                        '&sort=' + encodeURIComponent(sortBy.value) +
                        '&format=' + encodeURIComponent(exportFormat.value);
        window.location.href = exportUrl;
      });
    }

    // Rename tag buttons
    document.querySelectorAll('.btn-rename-tag').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var oldTag = btn.dataset.tag;
        var newTag = prompt('Enter new tag name for #' + oldTag + ':', oldTag);
        if (!newTag || newTag.trim() === '' || newTag.trim() === oldTag) return;

        fetch('/api/tags/rename', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCsrfToken()
          },
          body: JSON.stringify({ old_tag: oldTag, new_tag: newTag.trim() })
        })
        .then(function(res) { return res.json(); })
        .then(function(data) {
          if (data.success) {
            window.location.reload();
            return;
          }
          //< a rejected rename (401 after the session expired, 400 on a bad
          //  name) used to no-op silently, leaving the old name on screen with
          //  no hint that anything failed
          alert(data.error || "Couldn't rename that tag. Please try again.");
        })
        .catch(function(err) {
          console.error('Error renaming tag:', err);
          alert("Couldn't rename that tag. Please try again.");
        });
      });
    });

    // Delete tag buttons
    document.querySelectorAll('.btn-delete-tag').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var tag = btn.dataset.tag;
        if (!confirm('Are you sure you want to delete tag #' + tag + ' from all items?')) return;

        fetch('/api/tags/' + encodeURIComponent(tag), {
          method: 'DELETE',
          headers: {
            'X-CSRFToken': getCsrfToken()
          }
        })
        .then(function(res) { return res.json(); })
        .then(function(data) {
          if (data.success) {
            window.location.reload();
            return;
          }
          alert(data.error || "Couldn't delete that tag. Please try again.");
        })
        .catch(function(err) {
          console.error('Error deleting tag:', err);
          alert("Couldn't delete that tag. Please try again.");
        });
      });
    });
  });
})();
