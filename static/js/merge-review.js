// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

// The merge review queue's main-version picker. The page suggests which
// release should stay as the song's page (the plainest title - see
// _electCanonical), and this lets a person overrule that suggestion without a
// round trip: the radio moves the "kept as the song's page" badge and re-aims
// every merge form in the group at the newly picked release.
//
// The server already renders a correct, working page on its own - the
// suggested release badged, the others carrying their two verdict buttons. So
// with JavaScript off nothing here is needed to rule on a group; only the
// ability to change the suggestion is lost.

// Pure decision function: given every release id in one group and the id
// picked as the main version, what each row should look like. A pick naming
// no row in the group falls back to the first, which is the server's own
// suggestion - merging into an id that is not on offer is the one outcome
// this must not produce.
function mergeReviewRowStates(trackIds, mainTrackId) {
  if (!trackIds.length) {
    return [];
  }
  const main = trackIds.indexOf(mainTrackId) === -1 ? trackIds[0] : mainTrackId;
  return trackIds.map((trackId) => ({
    trackId,
    isMain: trackId === main,
    canonical: main,
  }));
}

function applyMergeReviewGroup(group) {
  const rows = Array.from(group.querySelectorAll('[data-merge-release]'));
  const picked = group.querySelector('[data-merge-main-radio]:checked');
  const states = mergeReviewRowStates(
    rows.map((row) => row.dataset.trackId),
    picked ? picked.value : null,
  );

  states.forEach((state, index) => {
    const row = rows[index];
    const badge = row.querySelector('[data-merge-main-badge]');
    const actions = row.querySelector('[data-merge-actions]');
    if (badge) {
      badge.hidden = !state.isMain;
    }
    if (actions) {
      // The main version has nothing to rule on: it cannot merge into itself,
      // and "not the same" for the release the group is BUILT around would
      // pin the anchor rather than answer the question being asked.
      actions.hidden = state.isMain;
    }
    // Both forms in the row carry one: the merge posts it as the target, and
    // the reject posts it as what the "no" was ruled AGAINST. A single-element
    // lookup left the second on whatever the server rendered, which nothing on
    // screen would have shown - a hidden field disagreeing with the radio.
    row.querySelectorAll('input[name="canonical"]').forEach((input) => {
      input.value = state.canonical;
    });
  });
}

function initMergeReview() {
  document.querySelectorAll('[data-merge-group]').forEach((group) => {
    // Applied once up front, not only on change: a browser restoring form
    // state on Back can hand back a radio the server did not render checked,
    // which would otherwise leave the badge and the forms describing a
    // different release than the one selected.
    applyMergeReviewGroup(group);
    group.addEventListener('change', (event) => {
      if (event.target.matches('[data-merge-main-radio]')) {
        applyMergeReviewGroup(group);
      }
    });
  });
}

if (typeof document !== 'undefined') {
  initMergeReview();
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { mergeReviewRowStates, applyMergeReviewGroup };
}
