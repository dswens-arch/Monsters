"""
pvp_engine.py
-------------
MONSTRS PvP — battle resolution engine.

Handles stat lookups, round-by-round 1v1 combat, and winner determination.
Completely standalone — no Discord imports, no side effects.
Called by pvp_cog.py.

Stat system:
  Each registered MONSTR has three trained stats stored in Supabase (monstr_pvp_stats).
  Base value: 10. Max: 50. Trait bonus applied once at registration (up to +5 per stat).

  ATTACK  — base damage per round
  DEFENSE — flat damage reduction each round
  SPEED   — determines who goes first; ties broken by coin flip

Combat (1v1):
  - Max 20 rounds (draw if neither dead)
  - Each round: faster MONSTR attacks first
  - Damage = max(1, attacker ATK - defender DEF) + random jitter ±20%
  - 8% crit chance → 1.75x damage
  - Winner = first to reduce opponent HP to 0

HP formula:
  base_hp = 80 + (defense * 3)
  (Higher defense = more HP to chip through, creating tank vs glass-cannon dynamic)
"""

import random
import math
from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────

MAX_ROUNDS        = 20
CRIT_CHANCE       = 0.08        # 8%
CRIT_MULTIPLIER   = 1.75
DAMAGE_JITTER     = 0.20        # ±20% random variance
BASE_HP_FLAT      = 80
BASE_HP_PER_DEF   = 3           # HP bonus per defense point

STAT_BASE         = 10
STAT_MAX          = 50
TRAIT_BONUS_MAX   = 5           # max bonus per stat from trait snapshot

# $GOO upgrade cost schedule: list of (min_level, cost_per_upgrade)
# Level here = current value of the stat (starts at 10)
UPGRADE_COSTS = [
    (41, 2000),
    (31, 1000),
    (21, 500),
    (11, 250),
    (1,  100),
]


# ─────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────

@dataclass
class MonstrStats:
    asa_id:       str
    name:         str            # e.g. "MONSTR #0675"
    owner_id:     str            # Discord user ID
    attack:       int = STAT_BASE
    defense:      int = STAT_BASE
    speed:        int = STAT_BASE
    image_url:    Optional[str] = None

    @property
    def hp(self) -> int:
        return BASE_HP_FLAT + (self.defense * BASE_HP_PER_DEF)


@dataclass
class RoundResult:
    round_num:    int
    attacker_id:  str            # asa_id of attacker
    defender_id:  str
    damage:       int
    is_crit:      bool
    defender_hp:  int            # HP remaining after this round
    flavor:       str            # one-line description


@dataclass
class BattleResult:
    winner_asa:   str
    loser_asa:    str
    winner_owner: str
    loser_owner:  str
    rounds:       list[RoundResult] = field(default_factory=list)
    is_draw:      bool = False
    total_rounds: int = 0


# ─────────────────────────────────────────────
# UPGRADE COST HELPER
# ─────────────────────────────────────────────

def upgrade_cost(current_stat_value: int) -> int:
    """Return the $GOO cost to upgrade a stat that is currently at current_stat_value."""
    for min_lvl, cost in UPGRADE_COSTS:
        if current_stat_value >= min_lvl:
            return cost
    return UPGRADE_COSTS[-1][1]


def can_upgrade(current_stat_value: int) -> bool:
    return current_stat_value < STAT_MAX


# ─────────────────────────────────────────────
# COMBAT ENGINE
# ─────────────────────────────────────────────

def _calc_damage(attacker: MonstrStats, defender: MonstrStats) -> tuple[int, bool]:
    """
    Returns (damage, is_crit).
    Base damage = max(1, ATK - DEF), then jitter, then maybe crit.
    """
    base   = max(1, attacker.attack - defender.defense)
    jitter = random.uniform(1 - DAMAGE_JITTER, 1 + DAMAGE_JITTER)
    dmg    = int(base * jitter)
    dmg    = max(1, dmg)

    is_crit = random.random() < CRIT_CHANCE
    if is_crit:
        dmg = int(dmg * CRIT_MULTIPLIER)

    return dmg, is_crit


def _flavor(attacker: MonstrStats, defender: MonstrStats, dmg: int, is_crit: bool, defender_hp: int) -> str:
    if is_crit:
        lines = [
            f"**{attacker.name}** lands a CRITICAL strike on **{defender.name}** for **{dmg} dmg!** 💥",
            f"**{attacker.name}** CRITS! **{dmg} damage** slams into **{defender.name}**! 💥",
            f"Critical hit! **{attacker.name}** unloads **{dmg} dmg** on **{defender.name}**! 💥",
        ]
    elif defender_hp <= 0:
        lines = [
            f"**{attacker.name}** finishes off **{defender.name}** with {dmg} damage. ☠️",
            f"**{attacker.name}** delivers the killing blow — **{dmg} damage**! ☠️",
            f"**{defender.name}** goes down! Final hit: {dmg} damage from **{attacker.name}**. ☠️",
        ]
    elif defender_hp < 20:
        lines = [
            f"**{attacker.name}** hits for {dmg} — **{defender.name}** is barely standing! ({defender_hp} HP)",
            f"{dmg} damage! **{defender.name}** is on the ropes. ({defender_hp} HP left)",
        ]
    else:
        lines = [
            f"**{attacker.name}** attacks for {dmg} damage. **{defender.name}** has {defender_hp} HP left.",
            f"**{attacker.name}** hits **{defender.name}** for {dmg}. ({defender_hp} HP remaining)",
            f"{dmg} damage dealt to **{defender.name}**. ({defender_hp} HP left)",
        ]
    return random.choice(lines)


def resolve_battle(a: MonstrStats, b: MonstrStats) -> BattleResult:
    """
    Run a full 1v1 battle between two MONSTRs.
    Returns a BattleResult with round-by-round log.
    """
    # Determine turn order — higher speed goes first, ties are coin flip
    if a.speed > b.speed:
        first, second = a, b
    elif b.speed > a.speed:
        first, second = b, a
    else:
        first, second = (a, b) if random.random() < 0.5 else (b, a)

    hp = {a.asa_id: a.hp, b.asa_id: b.hp}
    monstr = {a.asa_id: a, b.asa_id: b}
    rounds: list[RoundResult] = []

    for round_num in range(1, MAX_ROUNDS + 1):
        for attacker, defender in [(first, second), (second, first)]:
            if hp[defender.asa_id] <= 0 or hp[attacker.asa_id] <= 0:
                break

            dmg, is_crit = _calc_damage(attacker, defender)
            hp[defender.asa_id] -= dmg
            remaining = max(0, hp[defender.asa_id])

            fl = _flavor(attacker, defender, dmg, is_crit, remaining)
            rounds.append(RoundResult(
                round_num   = round_num,
                attacker_id = attacker.asa_id,
                defender_id = defender.asa_id,
                damage      = dmg,
                is_crit     = is_crit,
                defender_hp = remaining,
                flavor      = fl,
            ))

            if remaining <= 0:
                return BattleResult(
                    winner_asa   = attacker.asa_id,
                    loser_asa    = defender.asa_id,
                    winner_owner = attacker.owner_id,
                    loser_owner  = defender.owner_id,
                    rounds       = rounds,
                    is_draw      = False,
                    total_rounds = round_num,
                )

    # Draw after MAX_ROUNDS
    # Whoever has more HP remaining wins on points; true tie if equal
    hp_a = max(0, hp[a.asa_id])
    hp_b = max(0, hp[b.asa_id])

    if hp_a > hp_b:
        winner, loser = a, b
    elif hp_b > hp_a:
        winner, loser = b, a
    else:
        # Pure draw — refund both (caller handles)
        return BattleResult(
            winner_asa   = "",
            loser_asa    = "",
            winner_owner = "",
            loser_owner  = "",
            rounds       = rounds,
            is_draw      = True,
            total_rounds = MAX_ROUNDS,
        )

    return BattleResult(
        winner_asa   = winner.asa_id,
        loser_asa    = loser.asa_id,
        winner_owner = winner.owner_id,
        loser_owner  = loser.owner_id,
        rounds       = rounds,
        is_draw      = False,
        total_rounds = MAX_ROUNDS,
    )


# ─────────────────────────────────────────────
# STAT HELPERS (used by cog for display)
# ─────────────────────────────────────────────

def stat_bar(value: int, max_val: int = STAT_MAX, length: int = 10) -> str:
    """Return a text progress bar. e.g. ▓▓▓▓▓░░░░░"""
    filled = round((value / max_val) * length)
    return "▓" * filled + "░" * (length - filled)


def format_stats_embed_fields(stats: MonstrStats) -> list[tuple[str, str]]:
    """
    Returns list of (name, value) tuples for Discord embed fields.
    Used in /pvp_stats display.
    """
    atk_next = upgrade_cost(stats.attack)
    def_next = upgrade_cost(stats.defense)
    spd_next = upgrade_cost(stats.speed)

    return [
        ("⚔️ Attack",  f"`{stat_bar(stats.attack)}` **{stats.attack}** / {STAT_MAX}  •  next: {atk_next:,} $GOO"),
        ("🛡️ Defense", f"`{stat_bar(stats.defense)}` **{stats.defense}** / {STAT_MAX}  •  next: {def_next:,} $GOO"),
        ("⚡ Speed",   f"`{stat_bar(stats.speed)}` **{stats.speed}** / {STAT_MAX}  •  next: {spd_next:,} $GOO"),
        ("❤️ HP",      f"**{stats.hp}**  (base 80 + {stats.defense * BASE_HP_PER_DEF} from DEF)"),
    ]
