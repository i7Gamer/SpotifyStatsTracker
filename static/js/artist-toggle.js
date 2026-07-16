// "+N more" / "Show less" toggle on long artist lists. _artist_links.html
// renders lists longer than MAX_INLINE_ARTISTS with the tail inside a hidden
// .artist-overflow span next to an .artist-toggle button; this flips them.

// Pure decision function: given the button's current expanded state and how
// many names the overflow span holds, return the next state - what the
// button should say, and whether the overflow stays hidden.
function nextArtistToggleState(isExpanded, hiddenCount) {
  const expanded = !isExpanded;
  return {
    expanded,
    label: expanded ? 'Show less' : `+${hiddenCount} more`,
    overflowHidden: !expanded,
  };
}

function initArtistToggles() {
  // Delegated on document: the Compare and Wrapped pages swap whole card
  // lists via innerHTML (their AJAX filter controls), which would silently
  // drop per-button listeners.
  document.addEventListener('click', (event) => {
    const button = event.target.closest('.artist-toggle');
    if (!button) {
      return;
    }

    const links = button.closest('.artist-links');
    const overflow = links ? links.querySelector('.artist-overflow') : null;
    if (!overflow) {
      return;
    }

    const state = nextArtistToggleState(
      button.getAttribute('aria-expanded') === 'true',
      parseInt(button.dataset.hiddenCount, 10),
    );
    overflow.hidden = state.overflowHidden;
    button.textContent = state.label;
    button.setAttribute('aria-expanded', String(state.expanded));
  });
}

if (typeof document !== 'undefined') {
  initArtistToggles();
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { nextArtistToggleState };
}
