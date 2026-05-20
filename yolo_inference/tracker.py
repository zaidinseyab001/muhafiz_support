"""Stability filter over raw tracker ids.

Ultralytics' tracker can briefly invent ids for single-frame false
positives and can reassign ids during heavy occlusion. We promote a
track id to "stable" only after STABILITY_FRAMES consecutive sightings,
and we keep its state for up to STABILITY_GRACE missing frames so a
short occlusion does not reset it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Set

import config


@dataclass
class _TrackState:
    consecutive_seen: int = 0
    frames_missing: int = 0
    is_stable: bool = False
    class_id: int = -1


class StabilityFilter:
    def __init__(
        self,
        stability_frames: int = config.STABILITY_FRAMES,
        grace_frames: int = config.STABILITY_GRACE,
    ) -> None:
        self.stability_frames = stability_frames
        self.grace_frames = grace_frames
        self._states: Dict[int, _TrackState] = {}
        self._stable_ids_total: Set[int] = set()

    def update(
        self,
        current_track_ids: Iterable[int],
        id_to_class: Dict[int, int],
    ) -> dict:
        current = set(current_track_ids)

        for tid in current:
            state = self._states.setdefault(tid, _TrackState())
            state.consecutive_seen += 1
            state.frames_missing = 0
            state.class_id = id_to_class.get(tid, state.class_id)
            if not state.is_stable and state.consecutive_seen >= self.stability_frames:
                state.is_stable = True
                self._stable_ids_total.add(tid)

        for tid in list(self._states.keys()):
            if tid in current:
                continue
            state = self._states[tid]
            state.frames_missing += 1
            # Broken continuity — a not-yet-stable id has to climb again.
            state.consecutive_seen = 0
            if state.frames_missing > self.grace_frames:
                del self._states[tid]

        visible_stable_ids = sorted(
            tid for tid in current
            if tid in self._states and self._states[tid].is_stable
        )

        per_class_visible: Dict[int, int] = {}
        for tid in visible_stable_ids:
            cls = self._states[tid].class_id
            per_class_visible[cls] = per_class_visible.get(cls, 0) + 1

        return {
            "currently_visible_stable_ids": visible_stable_ids,
            "currently_visible_stable_count": len(visible_stable_ids),
            "currently_visible_stable_by_class": per_class_visible,
            "stable_total_unique": len(self._stable_ids_total),
            "raw_active_tracks": len(current),
        }
