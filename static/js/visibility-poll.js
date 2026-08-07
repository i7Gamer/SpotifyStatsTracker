// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

// A setInterval that runs only while somebody is looking at the tab.
//
// Every poll in this app used to run for the life of the tab. Browsers throttle
// background timers, they do not stop them, so a browser left open on any page
// kept asking: the topbar's listener pill every 10s, and on the dashboard the
// now-playing poll every 15s - and that one is not cheap, being a handful of
// queries for the viewer plus a fan-out over everyone they share with (see
// SpotifyDashboardApp.getFriendsNowPlaying).
//
// Coming back is the other half of it. A returning tab polls IMMEDIATELY rather
// than waiting out the rest of an interval, so what a user switches back to is
// current instead of being whatever was true when they left - which also makes
// this strictly better than the old always-on timer at the only moment anybody
// was actually reading the page.
//
// stop() is permanent, and that is load-bearing: both callers stop their poll
// on a 401, and a tab switch restarting it would put the app straight back to
// hammering the server on an expired session for the life of the tab.
(function () {
  function start(fn, intervalMs) {
    var timer = null;
    var stopped = false;

    function stopTimer() {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    }

    // Guarded on `timer`, not just on visibility: the browser fires
    // visibilitychange for other reasons too, and a second timer here would
    // mean two requests per interval for the rest of the page's life.
    function startTimer() {
      if (stopped || timer !== null) return;
      fn();
      timer = setInterval(fn, intervalMs);
    }

    document.addEventListener('visibilitychange', function () {
      if (document.hidden) stopTimer(); else startTimer();
    });

    //< a page opened in a background tab costs nothing until it is looked at
    if (!document.hidden) startTimer();

    return {
      stop: function () {
        stopped = true;
        stopTimer();
      },
    };
  }

  window.VisibilityPoll = { start: start };

  // Same feature detection as the other dual-use scripts here: a browser has no
  // `module`, so this is a no-op there. See tests/test_visibility_poll.js.
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { start: start };
  }
})();
