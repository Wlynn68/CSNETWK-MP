import json
import os
import re

from models.card_effects import (
    CARD_CATALOG,
    abilities_for,
    ability_targets_required,
    etb_for,
    get_registry,
    keywords_for,
    spell_effect_for,
    targets_required,
)

_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "jason files",
    "card_catalog.json",
)


def card_base_id(card_id: str) -> str:
    return re.sub(r"_\d+$", "", card_id)


def get_card(card_id: str) -> dict | None:
    base = card_base_id(card_id)
    card = CARD_CATALOG.get(base)
    if card is None:
        return None
    return {**card, "card_id": card_id}


def all_legal_card_ids() -> set[str]:
    ids = set()
    for base, data in CARD_CATALOG.items():
        copies = data.get("copies_in_set", 4)
        for i in range(1, copies + 1):
            ids.add(f"{base}_{i:03d}")
    return ids


def is_land(card_id: str) -> bool:
    card = get_card(card_id)
    return card is not None and card.get("card_type") == "Land"


def is_instant(card_id: str) -> bool:
    card = get_card(card_id)
    return card is not None and card.get("card_type") == "Instant"


def is_sorcery(card_id: str) -> bool:
    card = get_card(card_id)
    return card is not None and card.get("card_type") == "Sorcery"


def is_creature(card_id: str) -> bool:
    card = get_card(card_id)
    if card is None:
        return False
    return card.get("card_type") in ("Creature", "Artifact Creature")


def is_enchantment(card_id: str) -> bool:
    card = get_card(card_id)
    return card is not None and card.get("card_type") == "Enchantment"


def is_artifact(card_id: str) -> bool:
    card = get_card(card_id)
    if card is None:
        return False
    return card.get("card_type") in ("Artifact", "Artifact Creature")


def is_sorcery_speed(card_id: str) -> bool:
    return is_sorcery(card_id) or is_creature(card_id) or is_enchantment(card_id) or (
        is_artifact(card_id) and not is_creature(card_id)
    )


def can_cast_at_timing(card_id: str, phase: str) -> bool:
    if is_instant(card_id):
        return True
    if is_sorcery_speed(card_id):
        return phase in ("PRECOMBAT_MAIN", "POSTCOMBAT_MAIN")
    return False


def get_abilities(card_id: str) -> list[dict]:
    return abilities_for(card_id)


def is_basic_land_name(name: str) -> bool:
    return name.lower().startswith("basic ")


def find_basic_land_in_library(library: list[str]) -> str | None:
    for cid in library:
        c = get_card(cid)
        if c and c.get("card_type") == "Land" and is_basic_land_name(c.get("subtype") or ""):
            return cid
    return None
