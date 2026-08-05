"""
Derive spell effects, activated abilities, keywords, and ETB triggers
from card_catalog.json simplified_effect text.
"""

import json
import os
import re
from typing import Any

_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "jason files",
    "card_catalog.json",
)

with open(_CATALOG_PATH, encoding="utf-8") as _f:
    CARD_CATALOG: dict[str, dict] = json.load(_f)

COLOR_MAP = {"W": "W", "U": "U", "B": "B", "R": "R", "G": "G", "C": "C"}


def _parse_mana_braces(text: str) -> dict[str, int]:
    """Parse {R}, {1}{R}, {2}{U}{U} style costs from a fragment."""
    cost: dict[str, int] = {}
    for token in re.findall(r"\{([^}]+)\}", text):
        token = token.strip()
        if token in COLOR_MAP:
            cost[token] = cost.get(token, 0) + 1
        elif token.isdigit():
            cost["X"] = cost.get("X", 0) + int(token)
        elif token == "X":
            cost["X"] = cost.get("X", 0) + 1
    return cost


def _damage_amount(effect: str) -> int | None:
    m = re.search(r"deals (\d+) damage", effect, re.I)
    return int(m.group(1)) if m else None


def _build_spell_effect(base: str, card: dict) -> dict | None:
    effect = card.get("simplified_effect", "")
    ctype = card.get("card_type", "")

    if ctype == "Creature":
        return None

    dmg = _damage_amount(effect)
    if dmg is not None:
        if "any target" in effect.lower():
            return {"kind": "damage", "amount": dmg, "target": "any"}
        if "target player" in effect.lower():
            return {"kind": "damage", "amount": dmg, "target": "player"}
        if "target creature" in effect.lower():
            return {"kind": "damage", "amount": dmg, "target": "creature"}

    if re.search(r"counter target spell unless", effect, re.I):
        m = re.search(r"pays \{(\d+)\}", effect)
        pay = int(m.group(1)) if m else 3
        return {"kind": "counter_unless", "pay_generic": pay}

    if "counter target noncreature spell" in effect.lower():
        return {"kind": "counter", "filter": "noncreature"}

    if "counter target spell" in effect.lower():
        return {"kind": "counter", "filter": "any"}

    if "exile target creature" in effect.lower() and "gains life equal to its power" in effect.lower():
        return {"kind": "exile_creature", "gain_life_power": True}

    if "exile target creature" in effect.lower() and "search for a basic land" in effect.lower():
        return {"kind": "exile_creature", "fetch_land": True}

    if "return target creature to its owner's hand" in effect.lower():
        return {"kind": "bounce_creature"}

    if "destroy target nonartifact, nonblack creature" in effect.lower():
        return {"kind": "destroy_creature", "filter": "nonblack_nonartifact"}

    if "destroy target nonblack creature" in effect.lower():
        return {"kind": "destroy_creature", "filter": "nonblack"}

    if "destroy target artifact or enchantment" in effect.lower():
        return {"kind": "destroy_permanent", "types": ("Artifact", "Enchantment")}

    if "target creature gets +3/+3 until end of turn" in effect.lower():
        return {"kind": "pump", "power": 3, "toughness": 3}

    if "target creature can't be the target of spells or abilities your opponents control" in effect.lower():
        return {"kind": "hexproof_opponents", "target": "creature"}

    if "look at top 3" in effect.lower() and "draw a card" in effect.lower():
        return {"kind": "ponder"}

    if "search your library for a basic land" in effect.lower():
        return {"kind": "fetch_basic_land", "tapped": True}

    if "return target creature card from your graveyard to your hand" in effect.lower():
        return {"kind": "return_creature_gy_to_hand"}

    if "target player discards two cards" in effect.lower():
        return {"kind": "discard", "count": 2, "target": "player"}

    if re.search(r"add \{[WUBRGC]", effect, re.I) and ctype == "Instant":
        m = re.findall(r"\{([WUBRGC])\}", effect)
        pool = {}
        for c in m:
            pool[c] = pool.get(c, 0) + 1
        return {"kind": "ritual_mana", "pool": pool}

    if "target player gains 3 life" in effect.lower():
        return {"kind": "gain_life", "amount": 3, "target": "player"}

    if "enchant creature" in effect.lower() and "can't attack or block" in effect.lower():
        return {"kind": "aura", "enchant_type": "creature", "effect": "pacifism"}

    return None


def _build_abilities(base: str, card: dict) -> list[dict]:
    effect = card.get("simplified_effect", "")
    abilities: list[dict] = []

    # Tap: Add {R}.  (lands, elves, sol ring)
    m = re.match(r"^Tap:\s*Add(\s*\{[WUBRGC]\})+\.?", effect, re.I)
    if m or effect.startswith("Tap: Add"):
        colors = re.findall(r"\{([WUBRGC])\}", effect)
        if colors:
            abilities.append({
                "tap": True,
                "mana": {},
                "effect": "add_mana",
                "color": colors[0],
                "amount": len(colors),
            })
        return abilities

    # {2}, Tap: ...  or  {3}, Tap: ...
    for match in re.finditer(
        r"\{([^}]+)\}(?:\{([^}]+)\})?(?:,\s*Tap:\s*(.+?))(?:\.|$)",
        effect,
    ):
        pass  # handled below with simpler splits

    parts = re.split(r"(?<=\.)\s+", effect)
    for part in parts:
        part = part.strip()
        if not part:
            continue

        # {1}{G}: Regenerate
        regen = re.match(r"\{([^}]+)\}(?:\{([^}]+)\})*:\s*Regenerate", part, re.I)
        if regen:
            abilities.append({
                "tap": False,
                "mana": _parse_mana_braces(part.split(":")[0]),
                "effect": "regenerate_self",
            })
            continue

        # Tap: Draw a card, then discard
        if re.match(r"Tap:\s*Draw a card, then discard", part, re.I):
            abilities.append({"tap": True, "mana": {}, "effect": "loot"})
            continue

        # Tap: ... deals N damage to any target
        tap_dmg = re.match(r"Tap:\s*.+deals (\d+) damage to any target", part, re.I)
        if tap_dmg:
            abilities.append({
                "tap": True,
                "mana": {},
                "effect": "damage",
                "amount": int(tap_dmg.group(1)),
                "target": "any",
            })
            continue

        # Tap: Target creature you control gains protection
        if "Tap: Target creature you control gains protection" in part:
            abilities.append({
                "tap": True,
                "mana": {},
                "effect": "protection",
                "target": "friendly_creature",
            })
            continue

        # Tap: Destroy target tapped creature
        if "Tap: Destroy target tapped creature" in part:
            abilities.append({
                "tap": True,
                "mana": {},
                "effect": "destroy_creature",
                "filter": "tapped",
            })
            continue

        # {2}, Tap: Target player mills 2
        mill = re.search(r"(\{[^}]+\}(?:\{[^}]+\})*),\s*Tap:\s*Target player mills (\d+)", part, re.I)
        if mill:
            abilities.append({
                "tap": True,
                "mana": _parse_mana_braces(mill.group(1)),
                "effect": "mill",
                "amount": int(mill.group(2)),
                "target": "player",
            })
            continue

        # {3}, Tap: ... deals 1 damage to any target  (rod of ruin)
        rod = re.search(
            r"(\{[^}]+\}(?:\{[^}]+\})*),\s*Tap:\s*.+deals (\d+) damage to any target",
            part,
            re.I,
        )
        if rod:
            abilities.append({
                "tap": True,
                "mana": _parse_mana_braces(rod.group(1)),
                "effect": "damage",
                "amount": int(rod.group(2)),
                "target": "any",
            })
            continue

    return abilities


def _build_keywords(effect: str) -> list[str]:
    kw = []
    mapping = {
        "haste": "haste",
        "flying": "flying",
        "first strike": "first_strike",
        "vigilance": "vigilance",
        "trample": "trample",
        "hexproof": "hexproof",
        "defender": "defender",
        "prowess": "prowess",
        "protection from black": "protection_from_black",
        "protection from white": "protection_from_white",
    }
    lower = effect.lower()
    for needle, key in mapping.items():
        if needle in lower:
            kw.append(key)
    if "illusion" in lower and "sacrifice it" in lower:
        kw.append("phantasmal")
    return kw


def _build_etb(base: str, effect: str) -> dict | None:
    if "when gray merchant enters" in effect.lower():
        return {"kind": "gray_merchant_etb"}
    if "when gravedigger enters" in effect.lower():
        return {"kind": "gravedigger_etb", "needs_target": True}
    return None


# ── Build full registry at import ─────────────────────────────────────────────

CARD_REGISTRY: dict[str, dict[str, Any]] = {}

for _base, _card in CARD_CATALOG.items():
    _effect_text = _card.get("simplified_effect", "")
    CARD_REGISTRY[_base] = {
        "spell": _build_spell_effect(_base, _card),
        "abilities": _build_abilities(_base, _card),
        "keywords": _build_keywords(_effect_text),
        "etb": _build_etb(_base, _effect_text),
        "card_type": _card.get("card_type"),
        "is_artifact_creature": _card.get("card_type") == "Artifact Creature",
    }

# Fix sol ring / basic lands if parser missed
if not CARD_REGISTRY["sol_ring"]["abilities"]:
    CARD_REGISTRY["sol_ring"]["abilities"] = [
        {"tap": True, "mana": {}, "effect": "add_mana", "color": "C", "amount": 2}
    ]

for _land, _color in [("mountain", "R"), ("forest", "G"), ("island", "U"), ("plains", "W"), ("swamp", "B")]:
    CARD_REGISTRY[_land]["abilities"] = [
        {"tap": True, "mana": {}, "effect": "add_mana", "color": _color, "amount": 1}
    ]

# Ensure prodigal sorcerer / merfolk / mother / royal / millstone / rod parsed
if not CARD_REGISTRY["prodigal_sorcerer"]["abilities"]:
    CARD_REGISTRY["prodigal_sorcerer"]["abilities"] = [
        {"tap": True, "mana": {}, "effect": "damage", "amount": 1, "target": "any"}
    ]
if not CARD_REGISTRY["merfolk_looter"]["abilities"]:
    CARD_REGISTRY["merfolk_looter"]["abilities"] = [
        {"tap": True, "mana": {}, "effect": "loot"}
    ]
if not CARD_REGISTRY["mother_of_runes"]["abilities"]:
    CARD_REGISTRY["mother_of_runes"]["abilities"] = [
        {"tap": True, "mana": {}, "effect": "protection", "target": "friendly_creature"}
    ]
if not CARD_REGISTRY["royal_assassin"]["abilities"]:
    CARD_REGISTRY["royal_assassin"]["abilities"] = [
        {"tap": True, "mana": {}, "effect": "destroy_creature", "filter": "tapped"}
    ]
if not CARD_REGISTRY["millstone"]["abilities"]:
    CARD_REGISTRY["millstone"]["abilities"] = [
        {"tap": True, "mana": {"X": 2}, "effect": "mill", "amount": 2, "target": "player"}
    ]
if not CARD_REGISTRY["rod_of_ruin"]["abilities"]:
    CARD_REGISTRY["rod_of_ruin"]["abilities"] = [
        {"tap": True, "mana": {"X": 3}, "effect": "damage", "amount": 1, "target": "any"}
    ]
if not CARD_REGISTRY["llanowar_elves"]["abilities"]:
    CARD_REGISTRY["llanowar_elves"]["abilities"] = [
        {"tap": True, "mana": {}, "effect": "add_mana", "color": "G", "amount": 1}
    ]
if not CARD_REGISTRY["elvish_mystic"]["abilities"]:
    CARD_REGISTRY["elvish_mystic"]["abilities"] = [
        {"tap": True, "mana": {}, "effect": "add_mana", "color": "G", "amount": 1}
    ]
if not CARD_REGISTRY["troll_ascetic"]["abilities"]:
    CARD_REGISTRY["troll_ascetic"]["abilities"] = [
        {"tap": False, "mana": {"G": 1, "X": 1}, "effect": "regenerate_self"}
    ]

# Rift bolt same as bolt for resolution (suspend ignored in simplified rules)
for _burn in ("rift_bolt", "shock", "lightning_bolt", "searing_spear", "incinerate", "skullcrack"):
    if CARD_REGISTRY[_burn]["spell"] is None and _damage_amount(CARD_CATALOG[_burn]["simplified_effect"]):
        d = _damage_amount(CARD_CATALOG[_burn]["simplified_effect"])
        t = "any"
        if "target player" in CARD_CATALOG[_burn]["simplified_effect"].lower():
            t = "player"
        elif "target creature" in CARD_CATALOG[_burn]["simplified_effect"].lower():
            t = "creature"
        CARD_REGISTRY[_burn]["spell"] = {"kind": "damage", "amount": d, "target": t}


def get_registry(base: str) -> dict:
    return CARD_REGISTRY.get(base, {})

def spell_effect_for(card_id: str) -> dict | None:
    from models.cards import card_base_id
    return CARD_REGISTRY.get(card_base_id(card_id), {}).get("spell")

def abilities_for(card_id: str) -> list[dict]:
    from models.cards import card_base_id
    return CARD_REGISTRY.get(card_base_id(card_id), {}).get("abilities", [])

def keywords_for(card_id: str) -> list[str]:
    from models.cards import card_base_id
    return CARD_REGISTRY.get(card_base_id(card_id), {}).get("keywords", [])

def etb_for(card_id: str) -> dict | None:
    from models.cards import card_base_id
    return CARD_REGISTRY.get(card_base_id(card_id), {}).get("etb")

def targets_required(spell: dict | None) -> int:
    if not spell:
        return 0
    kind = spell.get("kind")
    if kind in ("damage", "bounce_creature", "destroy_creature", "destroy_permanent",
                "pump", "hexproof_opponents", "aura", "return_creature_gy_to_hand",
                "discard", "gain_life"):
        return 1
    if kind == "counter":
        return 1
    if kind == "counter_unless":
        return 1
    return 0

def ability_targets_required(ability: dict) -> int:
    kind = ability.get("effect")
    if kind in ("damage", "destroy_creature", "mill", "protection"):
        return 1
    return 0
