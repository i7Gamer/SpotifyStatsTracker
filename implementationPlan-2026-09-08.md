# Verified review implementation plan — 2026-09-08

Baseline: `0c926ed` (1.54.0). This plan supersedes the recommendations and priorities in the initial 2026-09-08 project review, incorporating `reviewVerification-2026-09-08.md`. It does not change the separate 2026-09-07 feature plan. The user has authorized implementation after an independent review of this plan.

## Evidence and scope

All eleven reported mechanisms remain valid. Severity describes impact and exposure, not certainty. The initial full baseline passed 6,550 tests, 4,961 subtests, with eight skips; Ruff and ESLint passed. Three additional isolated pytest probes passed on 2026-09-08: the original skip example, an allocation/display-name interleaving, and a counterexample to the proposed one-hop restoration. Temporary probes are evidence, not application changes.

The execution incident is closed **on the evidence supplied in the verification document**: the generated key was disposable, the existing setting predates the attempt, and the separate live installation was untouched. There is nothing to restore. No further live-data inspection is needed. Never construct the app outside pytest: its autouse fixtures must isolate database, media, keys, network and worker cleanup.

## Decisions on the proposed adjustments

| Finding | Revised priority | Decision |
| --- | --- | --- |
| 1. Email-less account adoption | P1 when an exposed NULL-email account exists; otherwise latent security defect | Accept suffixing occupied names. Live exposure check informs urgency, not permission to close the hole. |
| 2. Failed insert/cache acknowledgement | P3 | Prefer database-confirmed backfill deduplication and a small cache change; verify both caches before accepting the suggested filter. No new shutdown drain. |
| 3. Stale automatic merge plan | P2 | Accept short in-transaction revalidation; reject moving the expensive planner under the write lock. |
| 4. Revert onto moved targets | P3 | Accept lower priority and existing audit schema. Reject one-hop COALESCE as a general fix: a reproduced multiple-carried-decision case restores the wrong group. |
| 5. Callback failure hides incoming plays | P3 | Use the existing fallback record at the metadata callback boundary; preserve UID acknowledgement and avoid any new pending-event state. Poll mode is also affected by failure bursts. |
| 6. Duration/skip drift | P3 | Accept duplicate-gate repairs and existing admin remedy. Reject the claim that the original numeric example is wrong. Separate duration-triggered reclassification/cache work from the narrow duplicate fix, and label residual drift honestly. |
| 7. Case-insensitive identity reservation | P2 | Reuse the existing two-namespace predicate, atomically with allocation. A registry-only precheck does not serialize concurrent display-name changes. Preserve migration upsert semantics. |
| 8. Nested album rate limit | P3 | Accept a batch-loop cooldown check and partial results. More than one extra request is possible if following albums succeed; the exposure is bounded by the batch. |
| 9. Compare sort guard | P2/P3 | Accept the narrow sort-origin guard and parameter pruning. Preserve cheap sort requests. Failed full refresh followed by sort is a separate stale-render case, not necessarily an incomplete-date fallback. |
| 10. Genres locked full swap | P3 | Accept full-navigation HX-Redirect, preserving filters. The locked shell does not auto-request, so the claimed redirect loop does not apply. |
| 11. Wrapped genre preservation | P3 | Preserve the measured optimization and document its refresh boundary. Do not claim there is an existing cheap complete revision: app_settings has no update timestamp. |

## Implementation sequence and regression gates

Each coherent change must have a failing regression test first, the smallest sufficient implementation, and a targeted check that the test fails when the fix is removed. Run the full pytest suite, Ruff and ESLint before each implementation commit. Use descriptive commits; never push. Read files and callers before editing. Do not edit unrelated planning documents or access live data. Two cheaper, different models must challenge this plan before application edits.

### 1. Account ownership and name allocation (findings 1 and 7)

- Keep case-insensitive existing-email lookup first. Treat existing NULL-email rows as occupied; preserve their history, admin status, credentials and shares. Keep live-registry collisions and suffix allocation.
- Reserve username and display_name using the same NOCASE predicate as `setDisplayName`, inside the database write operation or a short transaction. Preserve `upsertUser` for migration/import callers. Do not add a global lock across network work.
- The existing `_session_lock` already serializes registry allocations. The missing interleaving is display-name reservation versus registration: availability check, existing user's successful rename, then new-user insertion. The probe reproduced duplicate displayed identities in that order.
- Preserve first-account admin bootstrap and configured ADMIN_EMAIL behavior. Use an explicit operator-controlled email reassociation procedure with ownership verification; do not add an unrequested admin feature merely to close the takeover hole.
- Tests: existing-email case variants, orphan ownership/history, occupied display and username case variants, suffix chains, cached users, blank sanitized prefix, first-user promotion, and a deterministic rename/allocation interleaving.

### 2. Merge revalidation and restoration (findings 3 and 4)

- Keep preview and expensive election scans outside `BEGIN IMMEDIATE`. Capture expected pointers as part of the original planning snapshot; the existing public plan currently omits them, so a later unlocked read is not an adequate baseline.
- Under the existing write transaction, batch-read current pointers and manual pins for planned members, elected heads and reheaded roots. Skip a group if its head moved or became pinned; skip moved/pinned members. If skipping a required rehead member invalidates the group, skip that group. Preserve the deliberate exemption for `manual-reject`.
- Retain the dependent re-reads and existing historical audit/carry semantics. Bound query parameters appropriately; do not promise literally two statements for arbitrarily large plans. Check that unrelated writers are not held behind planner scans.
- Revert must compute restoration destinations from the **final effective pointers**, after automatic edges are removed and all carried manual destinations are considered together. Use existing `canonical_id` and `carried_canonical_id`; no schema change or generic graph subsystem. Flatten affected destinations before applying; reject unsafe cycles/self-pointers without partially committing a revert. Preserve historical decision targets, return counts and invalidation.
- Concrete counterexample to one-hop SQL: manual C→B; automatic B→A carries C; manual B→D; automatic D→E carries B. Revert must end with B→D and C→D. The proposed SQL, when C is processed first, produced B→D and C→E. A flat graph alone is therefore not a sufficient assertion.
- Tests: concurrent split, member move, head move and head pin; reheading; manual-reject exemption; original and multiple-carried restoration sequences in different insertion orders; exact final groups, audit fields, no chains/self-pointers, rollback on unsafe topology, unchanged counts/cache scope.
- Count only applied groups after revalidation. Keep internal expected pointers out of public preview results. Disable-setting and revert must share the repository transaction, including rollback on rejected topology; two consecutive self-committing calls are insufficient.

### 3. Playback failure recovery (findings 2 and 5)

- When the existing database provider is supplied (always in production), build backfill deduplication solely from database-confirmed plays. Both observation caches can contain failed offers. A failed database lookup conservatively reoffers items; retain the old cache behavior only for callers with no provider. Keep the existing poll cadence and database duplicate protection.
- Test live-offer failure followed by backfill, consecutive failed backfills, mixed success/failure, successful deduplication, cache reset and replay-window bounds. Preserve exception isolation, health counters, poll cadence and stop latency. Do not create a persistence retry queue or drain on shutdown.
- In `Spotify._addToRecentlyPlayed`, catch `Exception` around `self.track(trackUri)` only; log the lookup failure and use `fallbackTrackRecord(normalizeSpotifyId(trackUri))`. Append the original start, context and elapsed milliseconds. Preserve `Spotify.track()` exceptions for other callers. No callback retry buffer, UID reordering, observation reset or shutdown flushing.
- Tests: repeated transient, local-rate-limit, closed-session and non-transient failures; multiple transitions through both push and poll; exact timestamps, context and elapsed milliseconds; one event per transition; later replacement of fallback metadata. Retain existing pause/seek/ad/stop tests. Residual: the preserved play may have placeholder metadata and approximate duration-dependent skip classification until metadata repair; this is preferable to losing it and does not promise durable buffering across process failure.

### 4. Skip duplicate repair and catalog cooldown (findings 6 and 8)

- Include `is_skip` in the duplicate-change decision in `insertPlay` and `_reconcileSingleMatch`. Preserve intentional sub-floor-twin exclusions, correction counters, immutable import identity, and the existing caller-owned transaction contracts of `upsertTrack`, `insertPlay` and `correctPlay`.
- The original example is valid at a 20% threshold: a 10-second play with unknown duration is initially non-skip (five-second fallback); after duration becomes 200 seconds it should be a skip (40-second threshold). The verification's reverse example, three seconds on a repaired 40-second track, needs a lower percentage such as 5%; it remains a skip at 20%.
- The narrow duplicate fix repairs replayed/reconciled rows only. Historical rows never replayed still need the existing admin skip-settings save. Document this limitation; per-track reclassification and broader Wrapped/milestone freshness are a separately tested follow-up, not silently declared solved. Preserve the measured unconditional bulk recompute and its rowcount.
- An is_skip-only import reconciliation must count as updated, propagate corrected years/earliest timestamp, invalidate the existing non-deferred import caches, and retain deferred invalidation state without committing. Listener duplicate freshness continues through existing worker checks; the narrow fix does not claim to refresh every cached view. Preserve creation metadata and correction counts.
- Check catalog cooldown at the top of each batch iteration. Preserve successful partial album tracks, repair eligibility, marking semantics, and the intentional separation from the listener limiter. Tests: first album succeeds then nested page 429; later successful mock albums must never be requested; expiry resumes; normal batches and direct 429 behavior unchanged.

### 5. UI consistency with existing query budgets (findings 9–11)

- Extend Compare's custom-date request veto **and** automatic-parameter pruning to `sortBy`; preserve counterpart badge navigation and the backend incomplete-custom/all-time rule. Keep the six-list sort optimization.
- On failed full-form requests, mark that a full refresh is required. A subsequent sort must request the full form endpoint once; clear the marker only on successful full refresh. Normal sorts keep the six-list endpoint. Test valid custom dates and counterpart changes, and preserve failure state after another failed recovery.
- Give full-target locked Genres responses the same filter-preserving HX-Redirect as detail responses. Update both tests that pin bare 204. Verify the redirect destination renders a locked shell without a deferred request.
- Keep Wrapped's same-year genre-card preservation as an explicit freshness/performance tradeoff. Correct route, template and test comments: year alone does not determine genres; reload/year navigation refreshes genre data. If adding a revision later, cover backfill, imports, merges, settings and threshold crossings without reintroducing the aggregation on every filter change.
- Tests: Compare sort/form veto, pruning, badge bypass, valid custom and normal sorting; Genres ready→locked transition and locked shell; Wrapped same-year preservation and year navigation. No browser preview.

## Limits and follow-ups

Live NULL-email exposure is unverified here; the maintainer may check it read-only on live, without blocking the code fix. Performance observations and incident closure supplied by the verification document are attributed evidence, not measurements repeated in this task. No review or passing suite can prove absence of all regressions; the implementation report must list checks actually run and any accepted residual behavior.

The duration-history refresh and complete Wrapped revision remain explicit follow-ups. Do not mark those mechanisms fully fixed on the strength of a narrower change.

## Independent plan review

Completed before application edits: GPT-5.6 Sol and GPT-5.6 Luna, both with high reasoning, independently challenged this plan and the source. Accepted corrections: atomic allocation against display-name changes; database-authoritative backfill; source-level metadata fallback instead of retention buffers; private planner guard state and applied counts; atomic disable/revert; explicit skip-only cache/transaction outcomes; concrete Compare failure recovery; complete Genres redirect assertions; and Wrapped test-comment corrections. Both approved subject to these incorporated corrections. No blocking design objection remains.

## Implementation record

The accepted scope is implemented in local commits; nothing was pushed:

| Commit | Result |
| --- | --- |
| `26fb1df` | Protect NULL-email accounts; reserve usernames atomically against both NOCASE namespaces. |
| `97775db` | Revalidate manual verdicts and original pointers; restore final merge roots and disable the toggle atomically. |
| `c2ac8b4` | Use database-confirmed backfill deduplication and preserve playback through metadata fallback. |
| `18c96dc` | Correct is_skip-only duplicates/import matches and stop catalog batches after nested cooldowns. |
| `a3159f6` | Include earlier changes in the same merge batch when revalidating later destinations. |
| `711170c` | Guard Compare sorts, recover failed/interrupted full requests, navigate locked Genres pages, and document Wrapped preservation. |

Additional regression cases found during implementation: a newly arrived unmerged plain release must still be eligible to replace a remaster head; an earlier automatic group must not move a later group's destination unnoticed. Both have reproductions and passing tests. The public preview shape stays unchanged and reports candidate counts, while apply reports only applied groups.

Eleven controlled mutation checks detected deliberately disabled fixes: atomic name reservation, manual-merge revalidation, final restoration destinations, database acknowledgement, metadata fallback, skip-only duplicate correction, both import transaction modes, catalog cooldown, Compare date guarding, Compare recovery, and Genres navigation. Every mutated file was restored in a finally block. Final clean validation: **6,573 tests passed, 4,975 subtests passed, eight tests skipped**, in 113.13 seconds; all **37 JavaScript test files**, **Ruff** and **ESLint** passed. Temporary reproduction tests and mutation scripts were removed; the committed regression tests remain.

Accepted residual behavior: duration repair alone does not rewrite historical skip flags or refresh every dependent cache; re-saving admin skip settings remains the existing bulk remedy. Placeholder playback metadata remains approximate until repaired. Same-year Wrapped presentation filters preserve the displayed genre card until reload/year navigation. Explicit legacy email reassociation remains an operator procedure requiring ownership verification, rather than a new admin UI. No live exposure check or production-data mutation was performed in this implementation.
