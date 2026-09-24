"""Interactive human driver for exact player-view Champions capture.

This module deliberately lets the human choose; it is not a policy. The inherited
VgcPlayer wrappers still record every request and exact wire choice, which gives the
post-game analyzer the same hindsight-safe input contract as bot-recorded games.
"""

from __future__ import annotations

from collections.abc import Callable

from poke_env.battle.abstract_battle import AbstractBattle
from poke_env.battle.double_battle import DoubleBattle
from poke_env.battle.pokemon import Pokemon
from poke_env.player.battle_order import BattleOrder, ForfeitBattleOrder

from vgc.actions import describe_order, enumerate_joint_orders
from vgc.agent import VgcPlayer

CHAMPIONS_PICK_COUNT = 4


def _species(mon: object) -> str:
    return str(getattr(mon, "species", None) or getattr(mon, "name", None) or "unknown")


def _hp_label(mon: object | None) -> str:
    if mon is None:
        return "(empty)"
    current = getattr(mon, "current_hp", None)
    maximum = getattr(mon, "max_hp", None)
    status = getattr(mon, "status", None)
    if isinstance(current, (int, float)) and isinstance(maximum, (int, float)) and maximum:
        base = f"{_species(mon)} {100.0 * current / maximum:.0f}%"
    else:
        base = _species(mon)
    if status:
        base += f" {status}"
    return base


def parse_preview_selection(
    raw: str,
    *,
    team_size: int,
    pick_count: int = CHAMPIONS_PICK_COUNT,
) -> tuple[int, ...] | None:
    """Parse a human preview entry such as 1234 or 1 2 3 4.

    Returns None for invalid input rather than silently substituting a team.
    """

    compact = "".join(char for char in raw if char.isdigit())
    if len(compact) != pick_count:
        return None
    slots = tuple(int(char) for char in compact)
    if len(set(slots)) != pick_count:
        return None
    if any(slot < 1 or slot > team_size for slot in slots):
        return None
    return slots


def parse_action_selection(raw: str, *, count: int) -> int | str | None:
    """Return a zero-based action index, the word forfeit, or None for invalid input."""

    value = raw.strip().lower()
    if value in {"q", "quit", "forfeit", "ff"}:
        return "forfeit"
    try:
        selected = int(value)
    except ValueError:
        return None
    if 1 <= selected <= count:
        return selected - 1
    return None


class HumanCapturePlayer(VgcPlayer):
    """VgcPlayer whose overridable hooks are driven by terminal/user input."""

    def __init__(
        self,
        *args,
        input_func: Callable[[str], str] = input,
        output_func: Callable[[str], None] = print,
        **kwargs,
    ) -> None:
        self._human_input = input_func
        self._human_output = output_func
        super().__init__(*args, **kwargs)

    def decide_teampreview(self, battle: AbstractBattle) -> str:
        members = list(getattr(battle, "team", {}).values())
        self._human_output("")
        self._human_output("TEAM PREVIEW")
        for index, member in enumerate(members, start=1):
            self._human_output(f"  {index}. {_species(member)}")

        while True:
            raw = self._human_input(
                f"Choose {CHAMPIONS_PICK_COUNT} slots in lead/order sequence "
                "(example 1234): "
            )
            slots = parse_preview_selection(raw, team_size=len(members))
            if slots is None:
                self._human_output("Invalid preview order. Use four distinct team slots.")
                continue
            for member in members:
                if isinstance(member, Pokemon):
                    member._selected_in_teampreview = False
            for slot in slots:
                member = members[slot - 1]
                if isinstance(member, Pokemon):
                    member._selected_in_teampreview = True
            return "/team " + "".join(str(slot) for slot in slots)

    def decide(self, battle: AbstractBattle) -> BattleOrder:
        if not isinstance(battle, DoubleBattle):
            return self.choose_random_move(battle)

        orders = enumerate_joint_orders(battle)
        if not orders:
            return self.choose_random_move(battle)

        turn = int(getattr(battle, "turn", 0) or 0)
        ours = list(getattr(battle, "active_pokemon", None) or [])
        theirs = list(getattr(battle, "opponent_active_pokemon", None) or [])
        self._human_output("")
        self._human_output(f"TURN {turn}")
        self._human_output(
            "  You: "
            + " | ".join(_hp_label(ours[index] if index < len(ours) else None) for index in range(2))
        )
        self._human_output(
            "  Foe: "
            + " | ".join(
                _hp_label(theirs[index] if index < len(theirs) else None) for index in range(2)
            )
        )
        force_switch = getattr(battle, "force_switch", False)
        forced = any(force_switch) if isinstance(force_switch, (list, tuple)) else bool(force_switch)
        if forced:
            self._human_output("  Forced replacement request")

        self._human_output("")
        for index, order in enumerate(orders, start=1):
            self._human_output(f"  {index:>3}. {describe_order(order)}")
        self._human_output("    q. Forfeit")

        while True:
            raw = self._human_input("Choose action: ")
            selected = parse_action_selection(raw, count=len(orders))
            if selected == "forfeit":
                return ForfeitBattleOrder()
            if isinstance(selected, int):
                return orders[selected]
            self._human_output(f"Invalid choice. Enter 1-{len(orders)} or q.")
