// Progressive "Show N more" reveal for the Profile > Milestones list
// (templates/profile.html). The server renders only the most recent milestone
// visible; the rest carry the `hidden` attribute next to a [data-milestone-more]
// button. This reveals them a chunk (data-chunk-size) at a time, and drops the
// button once everything is showing.

// Pure decision function: given how many items should currently be visible, the
// total number of items, and the chunk size, return the render state - how many
// end up visible (clamped to the total), what the button should say, and whether
// the button should disappear because nothing is left to reveal. Exported for
// the node test.
function milestoneRevealState(visibleCount, total, chunkSize) {
  const visible = Math.max(0, Math.min(visibleCount, total));
  const remaining = total - visible;
  return {
    visible,
    moreHidden: remaining <= 0,
    label: remaining > 0 ? `Show ${Math.min(chunkSize, remaining)} more` : '',
  };
}

function applyMilestoneReveal(items, moreBtn, visibleCount, chunkSize) {
  const state = milestoneRevealState(visibleCount, items.length, chunkSize);
  items.forEach((item, index) => { item.hidden = index >= state.visible; });
  moreBtn.hidden = state.moreHidden;
  if (state.label) {
    moreBtn.textContent = state.label;
  }
  return state.visible;
}

function initMilestoneMore() {
  const list = document.querySelector('[data-milestone-list]');
  const moreBtn = document.querySelector('[data-milestone-more]');
  if (!list || !moreBtn) {
    return;
  }
  const items = Array.from(list.children);
  const chunkSize = parseInt(moreBtn.dataset.chunkSize, 10);
  // The server renders the collapsed state, so the number of items it left
  // un-hidden IS the initial visible count - no magic number needed here.
  let visible = items.filter((item) => !item.hidden).length;
  visible = applyMilestoneReveal(items, moreBtn, visible, chunkSize);
  moreBtn.addEventListener('click', () => {
    visible = applyMilestoneReveal(items, moreBtn, visible + chunkSize, chunkSize);
  });
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initMilestoneMore);
  } else {
    initMilestoneMore();
  }
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { milestoneRevealState };
}
