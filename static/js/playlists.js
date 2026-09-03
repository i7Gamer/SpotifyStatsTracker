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
    //< UT-18 (2026-09-02 review): "Select one or more tags above..." used to
    //  stay on screen even once a real count sat right next to it
    var tagSelectionHint = document.getElementById('tagSelectionHint');

    var selectedTags = new Set();

    //< one tags= param per tag, never joined: a tag NAME may contain a comma
    //  (the server allows one), which a joined-then-split form cannot say -
    //  see routes/tags.py::_requestedTags, the other half of this protocol
    function tagsQueryString() {
      return Array.from(selectedTags).map(function (t) {
        return 'tags=' + encodeURIComponent(t);
      }).join('&');
    }

    //< the hidden input Flask-WTF renders; see the note in tags.js - the
    //  meta[name="csrf-token"] branch that used to lead this was dead in both
    //  copies, because nothing emits that tag
    function getCsrfToken() {
      var input = document.querySelector('input[name="csrf_token"]');
      return input ? input.value : '';
    }

    tagChips.forEach(function(chip) {
      //< a toggle button's pressed state belongs on aria-pressed, not only in
      //  the inline styles below - a screen reader has no way to read those
      chip.setAttribute('aria-pressed', 'false');
      chip.addEventListener('click', function() {
        var tag = chip.dataset.tag;
        var selected;
        if (selectedTags.has(tag)) {
          selectedTags.delete(tag);
          selected = false;
          chip.style.background = 'rgba(255,255,255,0.05)';
          chip.style.borderColor = 'var(--border-color, #444)';
          chip.style.color = 'inherit';
        } else {
          selectedTags.add(tag);
          selected = true;
          chip.style.background = 'color-mix(in srgb, var(--accent) 20%, transparent)';
          chip.style.borderColor = 'var(--accent)';
          chip.style.color = 'var(--accent)';
        }
        chip.setAttribute('aria-pressed', String(selected));
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
      //< owns the hint the same way it owns previewCount/btnDownload: visible
      //  exactly while there is nothing to preview
      if (tagSelectionHint) tagSelectionHint.hidden = selectedTags.size > 0;
      if (selectedTags.size === 0) {
        if (previewCount) previewCount.textContent = '0 tracks match selection';
        if (btnDownload) btnDownload.disabled = true;
        return;
      }

      var url = '/api/playlists/preview?' + tagsQueryString() +
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
            //< UT-18: the noun pluralised ("track"/"tracks") but the verb
            //  stayed plural regardless - "1 track match selection" reads
            //  wrong. A count of exactly one is the only singular subject.
            previewCount.textContent = cnt + ' track' + (cnt !== 1 ? 's' : '') +
              (cnt === 1 ? ' matches' : ' match') + ' selection';
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
        var exportUrl = '/playlist/export?' + tagsQueryString() +
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
