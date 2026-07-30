# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The owned Spotify client package (dependencyRewritePlan.md Phase 1) -
replaces the spotipyFree wrapper. See client.py for the surface, formatting.py
for the wire-shape mapping, recentlyPlayed.py for playback watching."""

from Database.Spotify.client import Spotify

__all__ = ["Spotify"]
