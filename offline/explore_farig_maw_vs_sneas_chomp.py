"""Run the opening-trajectory explorer for the confirmed Farigiraf+Mawile lead.

This is a thin matchup-lab entry point over ``explore_opening_trajectories``.  It adds
one forced preview plan discovered by the held-out lead screen without duplicating the
explorer's scoring/sampling implementation.
"""

from __future__ import annotations

import offline.explore_opening_trajectories as explorer
from offline.analyze_preview_matrix import ForcedPlan


FARIG_MAW_PLAN = ForcedPlan(
    "Farig+Maw | Tork+Kang | Mega Maw",
    "4613",
    "mawile",
    "Held-out lead-screen winner versus Sneasler+Garchomp; Farigiraf+Mawile lead with Torkoal+Kangaskhan back.",
)

# Keep the normal explorer implementation authoritative.  Extend its curated A-plan
# tuple only for this process, then expose a dedicated cell against B_QUICK[0]:
# Sneasler+Garchomp | Dragonite+Basculegion | Mega Dragonite.
explorer.A_QUICK = explorer.A_QUICK + (FARIG_MAW_PLAN,)
explorer.CELLS["farig-maw-vs-sneas-chomp"] = (len(explorer.A_QUICK) - 1, 0)


if __name__ == "__main__":
    raise SystemExit(explorer.main())
