# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Where a Top-list entry sat in the period before the one on screen.

"#3, up from #7 last month" is the question a ranked list invites and this one
could not answer. The comparison is against the equal-length span immediately
before the selected range, under the same sort and the same filters - anything
else compares two different questions.

Pure: the ordering comes from the SAME query the page itself ran, just against
the earlier window (see routes/charts.py's topListMovement). That is deliberate.
A hand-written "rank in the previous period" statement would be a second
spelling of three carefully-tuned ORDER BYs - tie-breakers included - and a
divergence between the two would show up as arrows that are quietly wrong,
which is worse than no arrows at all.

The cost of that choice is depth: we read the previous period's top
PREVIOUS_WINDOW_SCAN_LIMIT rather than all of it, so an entry that sat below
that line cannot be placed and gets no badge.

"New" is not left to that scan. Absence from a bounded list only ever meant
"the scan ended first" - and a year of one person's listening runs thousands of
entries deep, so on any range past a few months nothing could ever be called
new. The caller passes the ids that were played at all in the previous period
(Repository.getEntitiesPlayedInRange, an existence query cheap enough to ask
about a page's worth of entries), and absence from BOTH is what makes an entry
new."""

import datetime

# How deep into the previous period the comparison looks. The aggregate behind
# it costs the same whatever this is; what it buys is how often "not found"
# means "we know it wasn't there" instead of "we didn't look far enough", and
# what it costs is hydrating rows we only read an id from. 500 puts a typical
# month of one person's listening comfortably inside one scan.
PREVIOUS_WINDOW_SCAN_LIMIT = 500

UP = "up"
DOWN = "down"
SAME = "same"
NEW = "new"


def previousWindow(startDate: datetime.datetime | None, endDate: datetime.datetime | None):
    """The equal-length span immediately before [startDate, endDate), as
    (start, end) - or None when there is nothing to compare against.

    None is the All Time case, where startDate is unbounded: there is no
    "period before all of it", and inventing one (the same length as the user's
    whole history) would compare their listening against an empty prehistory
    and call every entry new."""
    if startDate is None or endDate is None:
        return None
    span = endDate - startDate
    if span <= datetime.timedelta(0):
        return None
    return startDate - span, startDate


def rankMovements(currentIds: list[str], previousIds: list[str],
                  startIndex: int = 0, playedPreviously: set | None = None) -> dict:
    """How far each of `currentIds` has moved, keyed by id::

        {"<id>": {"direction": "up"|"down"|"same"|"new", "amount": int}}

    `currentIds` is one page in rank order and `startIndex` is the rank above
    it, so page 3 compares real ranks rather than positions 1..20.
    `previousIds` is the previous period in the same order, deepest first -
    bounded by PREVIOUS_WINDOW_SCAN_LIMIT, so it places an entry or it does not.

    `playedPreviously` is what separates the two reasons an entry can be missing
    from that scan: the ids known to have been played in the previous period at
    all (see Repository.getEntitiesPlayedInRange). Absent from the scan AND from
    this set means genuinely new; absent from the scan but present here means it
    played too far down to place, which is not something to put a badge on.
    Passing None keeps every unplaceable entry silent, since without it "new"
    cannot be told apart from "we did not look far enough".

    An id with nothing to say is ABSENT from the result rather than present and
    empty - the caller renders one badge per entry it hears about, so silence
    stays distinguishable from "it did not move".

    A previous period with no plays at all yields nothing, whatever the rest
    says: every entry would otherwise be flagged new, which is a page of badges
    saying one thing about the period rather than anything about the entries."""
    if not previousIds:
        return {}

    previousRank = {entityId: index + 1 for index, entityId in enumerate(previousIds)}
    movements = {}
    for index, entityId in enumerate(currentIds):
        was = previousRank.get(entityId)
        if was is None:
            if playedPreviously is not None and entityId not in playedPreviously:
                movements[entityId] = {"direction": NEW, "amount": 0}
            continue
        rank = startIndex + index + 1
        #< a SMALLER rank number is a better position, so the subtraction is
        #  this way round: was #7, now #3 -> up 4
        moved = was - rank
        if moved > 0:
            movements[entityId] = {"direction": UP, "amount": moved}
        elif moved < 0:
            movements[entityId] = {"direction": DOWN, "amount": -moved}
        else:
            movements[entityId] = {"direction": SAME, "amount": 0}
    return movements
