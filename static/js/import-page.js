// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

// The import page's progress poller and its destructive-overwrite guard.
//
// Moved out of templates/import.html's <script> block so the lint gate reads it
// (see eslint.config.js): the `resp`/`response` ReferenceError class that gate
// exists for would, on THIS page, take out the confirm() standing in front of
// "deletes ALL recorded plays in the years these files cover".
//
// Reads IMPORT_PROGRESS_URL and IMPORT_IS_RUNNING off the data island the
// template still renders - same split as history.html / tracks.html.
(function () {
  var progressBar = document.getElementById('progress-bar');
  var progressMessage = document.getElementById('progress-message');
  var importForm = document.getElementById('import-form');

  var RUNNING_POLL_MS = 1200;   //< while an import is actually progressing
  var IDLE_POLL_MS = 5000;      //< queued/unknown: slower, it may not have started yet
  var UNAUTHORIZED_STATUS = 401;          //< the only non-2xx /import-progress answers by design
  var MAX_CONSECUTIVE_POLL_ERRORS = 5;    //< then say so, rather than poll a dead endpoint forever

  var consecutivePollErrors = 0;

  // Any failed request used to end the poll for the life of the page: one 502
  // from a proxy, or a few seconds offline, and the bar froze mid-import while
  // the import itself carried on - only a reload showed where it had got to.
  //
  // Retrying is not unconditional either. A poll that can never succeed must not
  // run until the tab closes, and stopping silently would leave exactly the
  // frozen bar this is here to fix, so it gives up out loud.
  function retryOrGiveUp() {
    consecutivePollErrors += 1;
    if (consecutivePollErrors >= MAX_CONSECUTIVE_POLL_ERRORS) {
      progressMessage.textContent = 'Lost contact with the import - reload the page to see where it got to.';
      return;
    }
    setTimeout(fetchProgress, IDLE_POLL_MS);
  }

  async function fetchProgress() {
    try {
      var response = await fetch(window.IMPORT_PROGRESS_URL);
      if (response.status === UNAUTHORIZED_STATUS) {
        //< the session is gone, so every retry is a guaranteed 401 - the same
        //  rule the other polls follow (see static/js/visibility-poll.js)
        return;
      }
      if (!response.ok) {
        retryOrGiveUp();
        return;
      }
      consecutivePollErrors = 0;   //< the cap counts CONSECUTIVE failures
      var progress = await response.json();
      progressBar.value = progress.percentage || 0;
      if (progress.status === 'running') {
        progressMessage.textContent = 'Import in progress: ' + progress.percentage + '% — ' + progress.message;
        setTimeout(fetchProgress, RUNNING_POLL_MS);
      } else if (progress.status === 'complete') {
        progressMessage.textContent = 'Import complete: ' + progress.message;
      } else if (progress.status === 'failed') {
        progressMessage.textContent = 'Import failed: ' + progress.message;
      } else {
        setTimeout(fetchProgress, IDLE_POLL_MS);
      }
    } catch (error) {
      console.error('Failed to load import progress', error);
      retryOrGiveUp();
    }
  }

  if (window.IMPORT_IS_RUNNING) {
    fetchProgress();
  }

  if (importForm) {
    importForm.addEventListener('submit', function (event) {
      var overwrite = document.getElementById('overwrite-range');
      if (overwrite && overwrite.checked && !confirm(
        'This deletes ALL recorded plays in the years these files cover - including live-tracked and ' +
        'manually added ones - then re-imports them fresh. This cannot be undone.\n\nContinue?'
      )) {
        event.preventDefault();
        return;
      }
      var fileCount = document.getElementById('history_file').files.length;
      progressMessage.textContent = fileCount > 1
        ? 'Starting import of ' + fileCount + ' files...'
        : 'Starting import...';
      progressBar.value = 0;
    });
  }
})();
