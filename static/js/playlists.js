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

    //< the hidden input Flask-WTF renders; see the note in tags.js - the
    //  meta[name="csrf-token"] branch that used to lead this was dead in both
    //  copies, because nothing emits that tag
    function getCsrfToken() {
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
        .then(function(res) {
          //< peeled off before the body, and BEFORE the token check below: a
          //  401 is news about the SESSION, which every in-flight preview
          //  shares, so whichever one notices acts on it. Read straight
          //  through, the route's `{"error": "Not logged in"}` PARSES and
          //  `data.track_count || 0` reads 0 - so an expired session printed
          //  "0 tracks match selection", a false statement about the user's
          //  own library, and disabled Download with no way back.
          if (window.AjaxStatus && window.AjaxStatus.redirectIfUnauthorized(res)) {
            return null;
          }
          return res.json();
        })
        .then(function(data) {
          if (!data) return;                    //< navigating to /login
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
        //< the shared login redirect, not the alert below: a 401 body carries
        //  "Not logged in" in the same `error` field a rejected rename uses,
        //  so reading it as one left the user staring at an alert on a page
        //  every later click would bounce off. tags.js does this for the same
        //  endpoint; the alert stays for verdicts the server actually made.
        .then(function(res) {
          return (window.AjaxStatus && window.AjaxStatus.redirectIfUnauthorized(res))
            ? null : res.json();
        })
        .then(function(data) {
          if (!data) return;   //< navigating to /login
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
        //< the shared login redirect - see the rename handler above for why a
        //  401 must not reach the alert
        .then(function(res) {
          return (window.AjaxStatus && window.AjaxStatus.redirectIfUnauthorized(res))
            ? null : res.json();
        })
        .then(function(data) {
          if (!data) return;   //< navigating to /login
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
