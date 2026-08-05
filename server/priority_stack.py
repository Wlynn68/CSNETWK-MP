"""
Priority, stack, combat sub-state machine, and spell/ability resolution engine.
Fully aligned with RFC 0001 MTGNP v1.0 Specification.
"""

import random
from models.card_effects import (
    ability_targets_required,
    etb_for,
    keywords_for,
    spell_effect_for,
    targets_required,
)
from models.cards import (
    can_cast_at_timing,
    find_basic_land_in_library,
    get_abilities,
    get_card,
    is_artifact,
    is_creature,
    is_enchantment,
    is_land,
)
from models.pdu import priority_grant, stack_push, stack_resolve

PRIORITY_TIME_MS = 60_000
MAIN_PHASES = ("PRECOMBAT_MAIN", "POSTCOMBAT_MAIN")


class PriorityStackEngine:
    def __init__(self, ctx):
        self.ctx = ctx
        self.stack: list[dict] = []
        self.priority_player: str | None = None
        self.priority_seq: int = 0
        self.passes_since_action: int = 0
        self._stack_counter = 0
        self._perm_counter = 0
        self._trigger_counter = 0
        self.land_played_this_turn: set[str] = set()
        self.pending_choice: dict | None = None
        self.last_phase_seq: int = 0

        # Combat tracking state
        self.combat_attackers: list[dict] = []  # [{"creature_id": str, "target": str}]
        self.combat_blockers: list[dict] = []   # [{"creature_id": str, "blocking_id": str}]
        self.damage_orders: dict[str, list[str]] = {}  # attacker_id -> list of blocker_ids

    def _new_stack_id(self) -> str:
        self._stack_counter += 1
        return f"stk_{self._stack_counter:02d}"

    def _new_trigger_id(self) -> str:
        self._trigger_counter += 1
        return f"trg_{self._trigger_counter:02d}"

    def new_perm_id(self) -> str:
        self._perm_counter += 1
        return f"perm_{self._perm_counter:02d}"

    def find_permanent(self, instance_id: str) -> tuple[str, dict] | None:
        for pid, pdata in self.ctx.game_data.items():
            for perm in pdata.get("battlefield", []):
                perm_id = perm.get("id", perm.get("instance_id"))
                if perm_id == instance_id or perm.get("instance_id") == instance_id:
                    return pid, perm
        return None

    def untap_all(self, pid: str):
        for perm in self.ctx.game_data[pid].get("battlefield", []):
            perm["tapped"] = False

    def _empty_mana_pools(self):
        for pdata in self.ctx.game_data.values():
            pdata["mana_pool"] = {}

    # Priority 

    def grant_priority(self, player_id: str | None = None):
        if player_id is None:
            player_id = self.ctx.active_player
        self.priority_player = player_id
        self.priority_seq = self.ctx.next_seq()
        self.ctx.send_to(player_id, priority_grant(player_id, self.priority_seq, PRIORITY_TIME_MS))
        print(f"[PRIORITY] Granted to {player_id} (seq={self.priority_seq})")

    def validate_priority_action(self, conn, msg, pid) -> bool:
        msg_seq = msg.get("seq_num", 0)
        if self.pending_choice:
            self.ctx.send_error(conn, "WRONG_PHASE",
                "Waiting for trigger/choice/combat decision response.", msg, msg_seq)
            return False
        if pid != self.priority_player:
            self.ctx.send_error(conn, "NOT_YOUR_PRIORITY",
                f"Priority belongs to {self.priority_player}, not {pid}.", msg, msg_seq)
            return False
        if msg_seq != self.priority_seq:
            self.ctx.send_error(conn, "STALE_ACTION",
                f"Priority token mismatch. Expected seq_num {self.priority_seq}, got {msg_seq}.",
                msg, msg_seq)
            return False
        return True

    def handle_priority_pass(self, conn, msg, pid):
        if not self.validate_priority_action(conn, msg, pid):
            return
        self.passes_since_action += 1
        if self.passes_since_action >= 2:
            self.passes_since_action = 0
            if self.stack:
                self.resolve_top()
                if not self.ctx.game_over and not self.pending_choice:
                    self.grant_priority(self.ctx.active_player)
            else:
                self.advance_after_both_pass()
        else:
            self.grant_priority(self.ctx.get_opponent(pid))

    #  State-Based Actions

    def check_state_based_actions(self) -> bool:
        """Check SBAs (life zero, lethal damage). Repeat until no changes."""
        sba_occurred = False
        pids = self.ctx.get_pids()
        if not pids:
            return False

        p1, p2 = pids[0], pids[1]
        l1, l2 = self.ctx.game_data[p1]["life"], self.ctx.game_data[p2]["life"]

        # Simultaneous life loss check 
        if l1 <= 0 and l2 <= 0:
            active = self.ctx.active_player
            winner = self.ctx.get_opponent(active)
            self.ctx.trigger_game_over("LIFE_ZERO", winner, active)
            return True
        elif l1 <= 0:
            self.ctx.trigger_game_over("LIFE_ZERO", p2, p1)
            return True
        elif l2 <= 0:
            self.ctx.trigger_game_over("LIFE_ZERO", p1, p2)
            return True

        # Check creature lethal damage & zero toughness
        for pid in pids:
            bf = self.ctx.game_data[pid].get("battlefield", [])
            to_remove = []
            for perm in bf:
                if is_creature(perm["card_id"]):
                    card = get_card(perm["card_id"])
                    tough = (card.get("toughness") or 0) + perm.get("pump_toughness", 0)
                    if tough <= 0 or perm.get("damage", 0) >= tough:
                        to_remove.append(perm)

            for perm in to_remove:
                bf.remove(perm)
                self.ctx.game_data[pid]["graveyard"].append(perm["card_id"])
                sba_occurred = True

        return sba_occurred

    # Triggers & Ordering

    def handle_trigger_choice_response(self, conn, msg, pid):
        if not self.pending_choice:
            self.ctx.send_error(conn, "ILLEGAL_ACTION", "No pending choice.", msg, msg.get("seq_num"))
            return
        if msg.get("seq_num") != self.pending_choice["seq_num"]:
            self.ctx.send_error(conn, "STALE_ACTION", "Choice seq mismatch.", msg, msg.get("seq_num"))
            return
        if pid != self.pending_choice["player_id"]:
            self.ctx.send_error(conn, "ILLEGAL_ACTION", "Not your choice.", msg, msg.get("seq_num"))
            return

        choice = self.pending_choice
        self.pending_choice = None
        accept = msg.get("accept", False)

        if choice["kind"] == "mana_leak":
            leak_item = choice["leak_item"]
            target_item = choice["target_item"]
            if accept:
                err = self._pay_mana(pid, {"X": choice["pay"]}, {"X": choice["pay"]})
                if err:
                    self.ctx.send_error(conn, "INSUFFICIENT_MANA", err, msg, msg.get("seq_num"))
                    self._counter_spell(target_item, leak_item, {})
                    self._finish_leak(leak_item)
                    return
                self._finish_leak(leak_item)
            else:
                self._counter_spell(target_item, leak_item, {})
                self._finish_leak(leak_item)

        self.ctx.broadcast_game_state()
        self.check_state_based_actions()
        if not self.ctx.game_over:
            self.grant_priority(self.ctx.active_player)

    def handle_trigger_order_response(self, conn, msg, pid):
        if not self.pending_choice or self.pending_choice.get("kind") != "trigger_order":
            self.ctx.send_error(conn, "ILLEGAL_ACTION", "No pending trigger ordering.", msg, msg.get("seq_num"))
            return
        if msg.get("seq_num") != self.pending_choice["seq_num"]:
            self.ctx.send_error(conn, "STALE_ACTION", "Sequence number mismatch.", msg, msg.get("seq_num"))
            return
        if pid != self.pending_choice["player_id"]:
            self.ctx.send_error(conn, "ILLEGAL_ACTION", "Not your choice.", msg, msg.get("seq_num"))
            return

        ordered_ids = msg.get("ordered_trigger_ids", [])
        expected = self.pending_choice["trigger_ids"]
        if sorted(ordered_ids) != sorted(expected):
            self.ctx.send_error(conn, "TRIGGER_ORDER_INVALID", "Must contain exact trigger IDs.", msg, msg.get("seq_num"))
            return

        triggers_dict = self.pending_choice["triggers_dict"]
        self.pending_choice = None

        # Push triggers onto stack in specified order (first in list resolved last)
        for trg_id in ordered_ids:
            item = triggers_dict[trg_id]
            stk_id = self._new_stack_id()
            self.stack.append({
                "stack_item_id": stk_id,
                "item_type": "TRIGGER_ABILITY",
                "source": item["source_id"],
                "targets": item.get("targets", []),
                "controller": pid,
                "card_id": item["source_id"],
                "trigger": item["trigger"],
            })
            self.ctx.broadcast(stack_push(stk_id, "TRIGGER_ABILITY", item["source_id"], item.get("targets", []), self.ctx.next_seq(), pid))

        self.ctx.broadcast_game_state()
        if not self.ctx.game_over:
            self.grant_priority(self.ctx.active_player)

    def _finish_leak(self, leak_item):
        self.ctx.game_data[leak_item["controller"]]["graveyard"].append(leak_item["card_id"])
        self._emit_resolve(leak_item["stack_item_id"], "RESOLVED", {})

    # Phase / Turn Transitions

    def begin_turn(self):
        self.land_played_this_turn.clear()
        self._empty_mana_pools()
        active = self.ctx.active_player
        self.untap_all(active)
        self._clear_until_eot_effects()
        self._broadcast_phase("UNTAP", "UPKEEP")
        self._broadcast_phase("UPKEEP", "DRAW")
        if self.ctx.turn_number > 1:
            pdata = self.ctx.game_data[active]
            if not pdata["library"]:
                self.ctx.trigger_game_over("DECK_EMPTY", self.ctx.get_opponent(active), active)
                return
            pdata["hand"].append(pdata["library"].pop(0))
        self._broadcast_phase("DRAW", "PRECOMBAT_MAIN")
        self.ctx.current_phase = "PRECOMBAT_MAIN"
        self.passes_since_action = 0
        self.ctx.broadcast_game_state()
        self.grant_priority(active)

    def _clear_until_eot_effects(self):
        for pdata in self.ctx.game_data.values():
            for perm in pdata.get("battlefield", []):
                perm.pop("pump_power", None)
                perm.pop("pump_toughness", None)
                perm.pop("hexproof_vs_opponents", None)
                perm.pop("protection_color", None)
                perm["damage"] = 0

    def advance_after_both_pass(self):
        phase = self.ctx.current_phase
        if phase == "PRECOMBAT_MAIN":
            self._broadcast_phase("PRECOMBAT_MAIN", "BEGIN_COMBAT")
            self.passes_since_action = 0
            self.grant_priority(self.ctx.active_player)

        elif phase == "BEGIN_COMBAT":
            self.last_phase_seq = self._broadcast_phase("BEGIN_COMBAT", "DECLARE_ATTACKERS")
            self.passes_since_action = 0
            # Wait for Active Player DECLARE_ATTACKERS PDU

        elif phase == "DECLARE_ATTACKERS":
            self.last_phase_seq = self._broadcast_phase("DECLARE_ATTACKERS", "DECLARE_BLOCKERS")
            self.passes_since_action = 0
            # Wait for Non-Active Player DECLARE_BLOCKERS PDU

        elif phase == "DECLARE_BLOCKERS":
            # Check if multi-blocked attackers exist
            has_multi = False
            for att in self.combat_attackers:
                att_id = att["creature_id"]
                blockers = [b["creature_id"] for b in self.combat_blockers if b["blocking_id"] == att_id]
                if len(blockers) >= 2:
                    has_multi = True
                    break

            if has_multi:
                self.last_phase_seq = self._broadcast_phase("DECLARE_BLOCKERS", "ASSIGN_DAMAGE_ORDER")
                self.passes_since_action = 0
                # Wait for ASSIGN_DAMAGE_ORDER PDU
            else:
                self._check_and_resolve_combat_damage()

        elif phase == "ASSIGN_DAMAGE_ORDER":
            self._check_and_resolve_combat_damage()

        elif phase == "FIRST_STRIKE_DAMAGE":
            self._resolve_regular_combat_damage()

        elif phase == "END_OF_COMBAT":
            self.combat_attackers.clear()
            self.combat_blockers.clear()
            self.damage_orders.clear()
            self._broadcast_phase("END_OF_COMBAT", "POSTCOMBAT_MAIN")
            self.passes_since_action = 0
            self.grant_priority(self.ctx.active_player)

        elif phase == "POSTCOMBAT_MAIN":
            self._broadcast_phase("POSTCOMBAT_MAIN", "END_STEP")
            self.passes_since_action = 0
            self.grant_priority(self.ctx.active_player)

        elif phase == "END_STEP":
            self.last_phase_seq = self._broadcast_phase("END_STEP", "CLEANUP")
            self._execute_cleanup()

        else:
            self.grant_priority(self.ctx.active_player)

    def _broadcast_phase(self, frm: str, to: str) -> int:
        self.ctx.current_phase = to
        seq = self.ctx.next_seq()
        self.ctx.broadcast({
            "type": "PHASE_TRANSITION",
            "seq_num": seq,
            "from_phase": frm,
            "to_phase": to,
            "active_player": self.ctx.active_player,
            "turn": self.ctx.turn_number,
        })
        return seq

    # Combat Handlers

    def handle_declare_attackers(self, conn, msg, pid):
        msg_seq = msg.get("seq_num", 0)
        if pid != self.ctx.active_player:
            self.ctx.send_error(conn, "ILLEGAL_ACTION", "Only Active Player may declare attackers.", msg, msg_seq)
            return
        if self.ctx.current_phase != "DECLARE_ATTACKERS":
            self.ctx.send_error(conn, "WRONG_PHASE", "Not in DECLARE_ATTACKERS step.", msg, msg_seq)
            return

        attackers = msg.get("attackers", [])
        bf = self.ctx.game_data[pid]["battlefield"]

        # Validate attackers
        for att in attackers:
            cid = att.get("creature_id")
            found = next((p for p in bf if p.get("id") == cid or p.get("instance_id") == cid), None)
            if not found:
                self.ctx.send_error(conn, "ILLEGAL_ACTION", f"Creature '{cid}' not found.", msg, msg_seq)
                return
            if not is_creature(found["card_id"]):
                self.ctx.send_error(conn, "ILLEGAL_ACTION", f"'{cid}' is not a creature.", msg, msg_seq)
                return
            if found.get("tapped"):
                self.ctx.send_error(conn, "ILLEGAL_ACTION", f"Creature '{cid}' is tapped.", msg, msg_seq)
                return
            if found.get("summoning_sick") and "haste" not in found.get("keywords", keywords_for(found["card_id"])):
                self.ctx.send_error(conn, "ILLEGAL_ACTION", f"Creature '{cid}' has summoning sickness.", msg, msg_seq)
                return

        # Tap declared attackers
        for att in attackers:
            cid = att.get("creature_id")
            found = next(p for p in bf if p.get("id") == cid or p.get("instance_id") == cid)
            found["tapped"] = True

        self.combat_attackers = attackers
        self.ctx.broadcast_game_state()

        if not attackers:
            # Skip to END_OF_COMBAT if no attackers declared
            self._broadcast_phase("DECLARE_ATTACKERS", "END_OF_COMBAT")
            self.passes_since_action = 0
            self.grant_priority(self.ctx.active_player)
        else:
            self.passes_since_action = 0
            self.grant_priority(self.ctx.active_player)

    def handle_declare_blockers(self, conn, msg, pid):
        msg_seq = msg.get("seq_num", 0)
        nap = self.ctx.get_opponent(self.ctx.active_player)
        if pid != nap:
            self.ctx.send_error(conn, "ILLEGAL_ACTION", "Only Non-Active Player may declare blockers.", msg, msg_seq)
            return
        if self.ctx.current_phase != "DECLARE_BLOCKERS":
            self.ctx.send_error(conn, "WRONG_PHASE", "Not in DECLARE_BLOCKERS step.", msg, msg_seq)
            return

        blockers = msg.get("blockers", [])
        bf = self.ctx.game_data[pid]["battlefield"]
        valid_att_ids = [a["creature_id"] for a in self.combat_attackers]

        for blk in blockers:
            cid = blk.get("creature_id")
            target_att = blk.get("blocking_id")
            found = next((p for p in bf if p.get("id") == cid or p.get("instance_id") == cid), None)
            if not found:
                self.ctx.send_error(conn, "ILLEGAL_ACTION", f"Blocker '{cid}' not found.", msg, msg_seq)
                return
            if not is_creature(found["card_id"]):
                self.ctx.send_error(conn, "ILLEGAL_ACTION", f"'{cid}' is not a creature.", msg, msg_seq)
                return
            if target_att not in valid_att_ids:
                self.ctx.send_error(conn, "ILLEGAL_ACTION", f"Target attacker '{target_att}' invalid.", msg, msg_seq)
                return

        self.combat_blockers = blockers
        self.ctx.broadcast_game_state()
        self.passes_since_action = 0
        self.grant_priority(self.ctx.active_player)

    def handle_assign_damage_order(self, conn, msg, pid):
        msg_seq = msg.get("seq_num", 0)
        if pid != self.ctx.active_player:
            self.ctx.send_error(conn, "ILLEGAL_ACTION", "Only Active Player assigns damage order.", msg, msg_seq)
            return
        if self.ctx.current_phase != "ASSIGN_DAMAGE_ORDER":
            self.ctx.send_error(conn, "WRONG_PHASE", "Not in ASSIGN_DAMAGE_ORDER step.", msg, msg_seq)
            return

        att_id = msg.get("attacker_id")
        order = msg.get("blocker_order", [])
        self.damage_orders[att_id] = order

        self.passes_since_action = 0
        self.grant_priority(self.ctx.active_player)

    def _check_and_resolve_combat_damage(self):
        # Check if first strike creatures exist
        has_first_strike = False
        all_participants = [a["creature_id"] for a in self.combat_attackers] + [b["creature_id"] for b in self.combat_blockers]

        for cid in all_participants:
            found = self.find_permanent(cid)
            if found:
                kw = keywords_for(found[1]["card_id"])
                if "first_strike" in kw or "double_strike" in kw:
                    has_first_strike = True
                    break

        if has_first_strike and self.ctx.current_phase != "FIRST_STRIKE_DAMAGE":
            self._broadcast_phase("DECLARE_BLOCKERS", "FIRST_STRIKE_DAMAGE")
            self._calculate_and_apply_combat_damage(first_strike_only=True)
            self.passes_since_action = 0
            self.grant_priority(self.ctx.active_player)
        else:
            self._broadcast_phase(self.ctx.current_phase, "COMBAT_DAMAGE")
            self._resolve_regular_combat_damage()

    def _resolve_regular_combat_damage(self):
        self._calculate_and_apply_combat_damage(first_strike_only=False)
        self._broadcast_phase("COMBAT_DAMAGE", "END_OF_COMBAT")
        self.passes_since_action = 0
        self.grant_priority(self.ctx.active_player)

    def _calculate_and_apply_combat_damage(self, first_strike_only: bool = False):
        ap = self.ctx.active_player
        nap = self.ctx.get_opponent(ap)
        damage_events = []
        creatures_died = []

        for att in self.combat_attackers:
            att_id = att["creature_id"]
            att_found = self.find_permanent(att_id)
            if not att_found:
                continue
            _, att_perm = att_found
            att_card = get_card(att_perm["card_id"])
            att_power = (att_card.get("power") or 0) + att_perm.get("pump_power", 0)
            att_kw = keywords_for(att_perm["card_id"])

            is_fs = "first_strike" in att_kw or "double_strike" in att_kw
            if first_strike_only and not is_fs:
                continue
            if not first_strike_only and "first_strike" in att_kw and "double_strike" not in att_kw:
                continue

            # Find blockers for this attacker
            blockers = [b["creature_id"] for b in self.combat_blockers if b["blocking_id"] == att_id]

            if not blockers:
                # Unblocked attacker deals damage to player
                if att_power > 0:
                    self.ctx.game_data[nap]["life"] -= att_power
                    damage_events.append({"source": att_id, "target": nap, "amount": att_power})
            else:
                # Blocked attacker deals damage to blockers
                ordered_blockers = self.damage_orders.get(att_id, blockers)
                rem_power = att_power

                for blk_id in ordered_blockers:
                    blk_found = self.find_permanent(blk_id)
                    if not blk_found:
                        continue
                    _, blk_perm = blk_found
                    blk_card = get_card(blk_perm["card_id"])
                    blk_power = (blk_card.get("power") or 0) + blk_perm.get("pump_power", 0)
                    blk_tough = (blk_card.get("toughness") or 0) + blk_perm.get("pump_toughness", 0)
                    blk_kw = keywords_for(blk_perm["card_id"])

                    # Blocker deals damage to attacker
                    blk_is_fs = "first_strike" in blk_kw or "double_strike" in blk_kw
                    if (first_strike_only and blk_is_fs) or (not first_strike_only and (not blk_is_fs or "double_strike" in blk_kw)):
                        if blk_power > 0:
                            att_perm["damage"] = att_perm.get("damage", 0) + blk_power
                            damage_events.append({"source": blk_id, "target": att_id, "amount": blk_power})

                    # Attacker deals damage to blocker
                    if rem_power > 0:
                        dmg_to_assign = min(rem_power, blk_tough) if blk_id != ordered_blockers[-1] else rem_power
                        blk_perm["damage"] = blk_perm.get("damage", 0) + dmg_to_assign
                        damage_events.append({"source": att_id, "target": blk_id, "amount": dmg_to_assign})
                        rem_power -= dmg_to_assign

        # SBAs check
        for pid in self.ctx.get_pids():
            bf = self.ctx.game_data[pid]["battlefield"]
            dead = []
            for perm in bf:
                if is_creature(perm["card_id"]):
                    c = get_card(perm["card_id"])
                    t = (c.get("toughness") or 0) + perm.get("pump_toughness", 0)
                    perm_id = perm.get("id", perm.get("instance_id"))
                    if perm.get("damage", 0) >= t and t > 0:
                        dead.append(perm)
                        creatures_died.append(perm_id)

            for d in dead:
                bf.remove(d)
                self.ctx.game_data[pid]["graveyard"].append(d["card_id"])

        seq = self.ctx.next_seq()
        self.ctx.broadcast({
            "type": "COMBAT_DAMAGE_RESULT",
            "seq_num": seq,
            "damage_events": damage_events,
            "life_totals": {p: self.ctx.game_data[p]["life"] for p in self.ctx.get_pids()},
            "creatures_died": creatures_died,
        })
        self.ctx.broadcast_game_state()
        self.check_state_based_actions()

    # Cleanup Hand Discard 

    def _execute_cleanup(self):
        active = self.ctx.active_player
        hand = self.ctx.game_data[active]["hand"]

        if len(hand) > 7:
            # Must await DISCARD PDU from Active Player
            self.ctx.broadcast_game_state()
        else:
            self._finish_cleanup()

    def handle_discard(self, conn, msg, pid):
        msg_seq = msg.get("seq_num", 0)
        active = self.ctx.active_player
        if pid != active:
            self.ctx.send_error(conn, "ILLEGAL_ACTION", "Only Active Player discards during Cleanup.", msg, msg_seq)
            return
        if self.ctx.current_phase != "CLEANUP":
            self.ctx.send_error(conn, "WRONG_PHASE", "DISCARD only allowed during CLEANUP.", msg, msg_seq)
            return

        to_discard = msg.get("card_ids", [])
        hand = self.ctx.game_data[pid]["hand"]

        for cid in to_discard:
            if cid not in hand:
                self.ctx.send_error(conn, "ILLEGAL_ACTION", f"Card '{cid}' not in hand.", msg, msg_seq)
                return

        for cid in to_discard:
            hand.remove(cid)
            self.ctx.game_data[pid]["graveyard"].append(cid)

        self.ctx.broadcast_game_state()

        if len(hand) > 7:
            # Need more discards
            return
        else:
            self._finish_cleanup()

    def _finish_cleanup(self):
        self._clear_until_eot_effects()
        self._empty_mana_pools()
        pids = self.ctx.get_pids()
        active = self.ctx.active_player
        self.ctx.active_player = pids[(pids.index(active) + 1) % len(pids)]
        self.ctx.turn_number += 1
        self.ctx.broadcast_game_state()
        if not self.ctx.check_win_conditions():
            self.begin_turn()

    # PLAY_LAND

    def handle_play_land(self, conn, msg, pid):
        msg_seq = msg.get("seq_num", 0)
        card_id = msg.get("card_id")

        if msg_seq != self.priority_seq:
            self.ctx.send_error(conn, "STALE_ACTION", f"Priority token mismatch. Expected seq_num {self.priority_seq}, got {msg_seq}.", msg, msg_seq)
            return
        if pid != self.ctx.active_player:
            self.ctx.send_error(conn, "ILLEGAL_ACTION", "Only active player may play a land.", msg, msg_seq)
            return
        if self.ctx.current_phase not in MAIN_PHASES:
            self.ctx.send_error(conn, "WRONG_PHASE", "Lands only in main phases.", msg, msg_seq)
            return
        if pid in self.land_played_this_turn:
            self.ctx.send_error(conn, "ILLEGAL_ACTION", "Already played a land this turn.", msg, msg_seq)
            return
        if card_id not in self.ctx.game_data[pid]["hand"]:
            self.ctx.send_error(conn, "ILLEGAL_ACTION", f"'{card_id}' not in hand.", msg, msg_seq)
            return
        if not is_land(card_id):
            self.ctx.send_error(conn, "ILLEGAL_ACTION", f"'{card_id}' is not a land.", msg, msg_seq)
            return

        self.ctx.game_data[pid]["hand"].remove(card_id)
        perm = self._make_permanent(card_id, summoning_sickness=False)
        self.ctx.game_data[pid]["battlefield"].append(perm)
        self.land_played_this_turn.add(pid)
        self.ctx.broadcast_game_state()
        self.grant_priority(pid)

    def _make_permanent(self, card_id: str, summoning_sickness: bool = True) -> dict:
        perm_id = card_id  # Matches card instance ID from deck list as per RFC 0001
        return {
            "id": perm_id,
            "instance_id": perm_id,
            "card_id": card_id,
            "tapped": False,
            "summoning_sick": summoning_sickness,
            "damage": 0,
            "keywords": keywords_for(card_id),
        }

    # CAST_SPELL

    def handle_cast_spell(self, conn, msg, pid):
        if not self.validate_priority_action(conn, msg, pid):
            return
        card_id = msg.get("card_id")
        targets = msg.get("targets", [])
        mana_payment = msg.get("mana_payment", {})
        card = get_card(card_id)
        if card is None:
            self.ctx.send_error(conn, "ILLEGAL_ACTION", f"Unknown card '{card_id}'.", msg, msg.get("seq_num"))
            return
        if card_id not in self.ctx.game_data[pid]["hand"]:
            self.ctx.send_error(conn, "ILLEGAL_ACTION", "Card not in hand.", msg, msg.get("seq_num"))
            return
        if not can_cast_at_timing(card_id, self.ctx.current_phase):
            self.ctx.send_error(conn, "WRONG_PHASE", f"Cannot cast during {self.ctx.current_phase}.", msg, msg.get("seq_num"))
            return

        spell = spell_effect_for(card_id)
        card_type = card.get("card_type")
        is_permanent_spell = is_creature(card_id) or card_type == "Artifact" or card_type == "Artifact Creature"
        if spell is None and not is_permanent_spell:
            self.ctx.send_error(conn, "ILLEGAL_ACTION", f"Cannot cast {card_id}.", msg, msg.get("seq_num"))
            return

        err = self._validate_spell_targets(pid, card_id, spell, targets, is_permanent_spell)
        if err:
            self.ctx.send_error(conn, "ILLEGAL_TARGET", err, msg, msg.get("seq_num"))
            return

        err = self._pay_mana(pid, card.get("mana_cost", {}), mana_payment)
        if err:
            self.ctx.send_error(conn, "INSUFFICIENT_MANA", err, msg, msg.get("seq_num"))
            return

        self._on_spell_cast(pid, card_id)
        self.ctx.game_data[pid]["hand"].remove(card_id)
        item_id = self._new_stack_id()
        self.stack.append({
            "stack_item_id": item_id,
            "item_type": "SPELL",
            "source": card_id,
            "targets": targets,
            "controller": pid,
            "card_id": card_id,
        })
        self.passes_since_action = 0
        self.ctx.broadcast(stack_push(item_id, "SPELL", card_id, targets, self.ctx.next_seq(), pid))
        self.ctx.broadcast_game_state()
        self.grant_priority(self.ctx.get_opponent(pid))

    def _on_spell_cast(self, pid: str, card_id: str):
        if is_creature(card_id):
            return
        for perm in self.ctx.game_data[pid].get("battlefield", []):
            if "prowess" in perm.get("keywords", keywords_for(perm["card_id"])):
                perm["pump_power"] = perm.get("pump_power", 0) + 1
                perm["pump_toughness"] = perm.get("pump_toughness", 0) + 1

    # ACTIVATE_ABILITY

    def handle_activate_ability(self, conn, msg, pid):
        if not self.validate_priority_action(conn, msg, pid):
            return
        source_id = msg.get("source_id")
        ability_index = msg.get("ability_index", 0)
        targets = msg.get("targets", [])
        cost_payment = msg.get("cost_payment", {})
        found = self.find_permanent(source_id)
        if found is None:
            self.ctx.send_error(conn, "ILLEGAL_ACTION", "Permanent not found.", msg, msg.get("seq_num"))
            return
        owner, perm = found
        if owner != pid:
            self.ctx.send_error(conn, "ILLEGAL_ACTION", "You don't control that permanent.", msg, msg.get("seq_num"))
            return

        abilities = get_abilities(perm["card_id"])
        if ability_index >= len(abilities):
            self.ctx.send_error(conn, "ILLEGAL_ACTION", "Invalid ability index.", msg, msg.get("seq_num"))
            return

        ability = abilities[ability_index]
        if ability.get("tap") and perm.get("tapped"):
            self.ctx.send_error(conn, "ILLEGAL_ACTION", "Already tapped.", msg, msg.get("seq_num"))
            return

        err = self._validate_ability_targets(pid, ability, targets)
        if err:
            self.ctx.send_error(conn, "ILLEGAL_TARGET", err, msg, msg.get("seq_num"))
            return

        mana_cost = ability.get("mana", {})
        err = self._pay_mana(pid, mana_cost, cost_payment.get("mana", {}),
                             perm if ability.get("tap") else None, ability.get("tap", False))
        if err:
            self.ctx.send_error(conn, "INSUFFICIENT_MANA", err, msg, msg.get("seq_num"))
            return

        item_id = self._new_stack_id()
        self.stack.append({
            "stack_item_id": item_id,
            "item_type": "ABILITY",
            "source": source_id,
            "targets": targets,
            "controller": pid,
            "card_id": perm["card_id"],
            "ability_index": ability_index,
        })
        self.passes_since_action = 0
        self.ctx.broadcast(stack_push(item_id, "ABILITY", source_id, targets, self.ctx.next_seq(), pid))
        self.ctx.broadcast_game_state()
        self.grant_priority(self.ctx.get_opponent(pid))

    # Mana 

    def _available_mana_sources(self, pid: str, exclude_perm: dict | None = None) -> list[tuple[dict, str]]:
        sources = []
        for perm in self.ctx.game_data[pid].get("battlefield", []):
            if perm.get("tapped"):
                continue
            perm_id = perm.get("id", perm.get("instance_id"))
            if exclude_perm and perm_id == exclude_perm.get("id", exclude_perm.get("instance_id")):
                continue
            for ab in get_abilities(perm["card_id"]):
                if ab.get("effect") == "add_mana":
                    color = ab.get("color", "C")
                    for _ in range(ab.get("amount", 1)):
                        sources.append((perm, color))
        return sources

    def _pay_mana(self, pid: str, cost: dict, payment: dict,
                  tap_perm: dict | None = None, do_tap: bool = False) -> str | None:
        required = {k: int(v) for k, v in cost.items()}
        paid = {k: int(v) for k, v in payment.items()}
        if sum(paid.values()) < sum(required.values()):
            return f"Insufficient mana: need {sum(required.values())}, paid {sum(paid.values())}."
        for color, amount in required.items():
            if color != "X" and paid.get(color, 0) < amount:
                return f"Insufficient {color} mana."

        if sum(paid.values()) == 0 and not do_tap:
            return None

        pool = self.ctx.game_data[pid].setdefault("mana_pool", {})
        pool_needed = dict(paid)
        for color in ("W", "U", "B", "R", "G", "C"):
            need = pool_needed.get(color, 0)
            have = pool.get(color, 0)
            use = min(need, have)
            pool[color] = have - use
            pool_needed[color] = need - use

        generic = pool_needed.get("X", 0) + sum(max(0, pool_needed.get(c, 0)) for c in ("W", "U", "B", "R", "G", "C"))
        colorless = pool.get("C", 0)
        use_c = min(generic, colorless)
        pool["C"] = colorless - use_c
        generic -= use_c
        pool_needed["X"] = generic

        sources = self._available_mana_sources(pid, exclude_perm=tap_perm if do_tap else None)
        tapped_ids: set[str] = set()
        if do_tap and tap_perm:
            tap_perm["tapped"] = True
            tapped_ids.add(tap_perm.get("id", tap_perm.get("instance_id")))

        for color in ("W", "U", "B", "R", "G", "C"):
            need = pool_needed.get(color, 0)
            while need > 0:
                src = next((s for s in sources if s[0].get("id", s[0].get("instance_id")) not in tapped_ids and s[1] == color), None)
                if src is None:
                    src = next((s for s in sources if s[0].get("id", s[0].get("instance_id")) not in tapped_ids), None)
                if src is None:
                    if do_tap and tap_perm:
                        tap_perm["tapped"] = False
                    return f"Cannot pay mana: short on {color}."
                src[0]["tapped"] = True
                tapped_ids.add(src[0].get("id", src[0].get("instance_id")))
                need -= 1
            pool_needed[color] = 0

        while pool_needed.get("X", 0) > 0:
            src = next((s for s in sources if s[0].get("id", s[0].get("instance_id")) not in tapped_ids), None)
            if src is None:
                if do_tap and tap_perm:
                    tap_perm["tapped"] = False
                return "Cannot pay generic mana."
            src[0]["tapped"] = True
            tapped_ids.add(src[0].get("id", src[0].get("instance_id")))
            pool_needed["X"] -= 1
        return None

    # Target Validation

    def _validate_spell_targets(self, pid: str, card_id: str, spell: dict | None, targets: list, is_permanent: bool = False) -> str | None:
        etb = etb_for(card_id)
        if is_permanent:
            if etb and etb.get("needs_target"):
                if len(targets) != 1:
                    return "This creature requires a creature target in graveyard."
                return self._validate_gy_creature(targets[0], pid)
            if spell and spell.get("kind") == "aura":
                if len(targets) != 1:
                    return "Aura requires a creature target."
                return self._validate_creature_target(targets[0])
            if spell is None and not is_enchantment(card_id):
                return None if len(targets) == 0 else "This spell takes no targets."
            need = targets_required(spell)
            if len(targets) != need:
                return f"Expected {need} target(s), got {len(targets)}."
            if need == 0:
                return None

        if spell is None:
            if is_creature(card_id):
                return None
            return f"No effect defined for {card_id}."

        need = targets_required(spell)
        if len(targets) != need:
            return f"Expected {need} target(s), got {len(targets)}."
        if need == 0:
            return None

        t = targets[0]
        kind = spell.get("kind")
        if kind in ("counter", "counter_unless"):
            if not any(s["stack_item_id"] == t and s["item_type"] == "SPELL" for s in self.stack):
                return f"No spell '{t}' on stack."
            if kind == "counter" and spell.get("filter") == "noncreature":
                item = next(s for s in self.stack if s["stack_item_id"] == t)
                if is_creature(item.get("card_id", "")):
                    return "Negate only counters noncreature spells."
            return None
        if kind == "damage":
            return self._validate_damage_target(t, spell.get("target", "any"))
        if kind in ("bounce_creature", "destroy_creature", "pump", "hexproof_opponents", "aura"):
            return self._validate_creature_target(t, pid if kind == "hexproof_opponents" else None)
        if kind == "destroy_permanent":
            return self._validate_destroy_permanent(t, spell.get("types", ()))
        if kind == "return_creature_gy_to_hand":
            return self._validate_gy_creature(t, pid)
        if kind in ("discard", "gain_life", "mill"):
            if t not in self.ctx.get_pids():
                return "Invalid player target."
            return None
        if kind == "exile_creature":
            return self._validate_creature_target(t)
        return None

    def _validate_ability_targets(self, pid: str, ability: dict, targets: list) -> str | None:
        need = ability_targets_required(ability)
        if len(targets) != need:
            return f"Ability needs {need} target(s)."
        if need == 0:
            return None
        t = targets[0]
        eff = ability.get("effect")
        if eff == "damage":
            return self._validate_damage_target(t, ability.get("target", "any"))
        if eff == "destroy_creature":
            return self._validate_creature_target(t, filter_tapped=True)
        if eff == "mill":
            return None if t in self.ctx.get_pids() else "Invalid player."
        if eff == "protection":
            found = self.find_permanent(t)
            if not found or found[0] != pid:
                return "Must target a creature you control."
            return None
        return None

    def _validate_damage_target(self, target: str, kind: str) -> str | None:
        if kind == "player":
            return None if target in self.ctx.get_pids() else "Invalid player."
        if kind == "creature":
            return self._validate_creature_target(target)
        if target in self.ctx.get_pids():
            return None
        return self._validate_creature_target(target)

    def _validate_creature_target(self, iid: str, controller: str | None = None, filter_tapped: bool = False) -> str | None:
        found = self.find_permanent(iid)
        if not found:
            return f"Creature '{iid}' not found."
        owner, perm = found
        if controller and owner != controller:
            return "Wrong controller for target."
        if not is_creature(perm["card_id"]):
            return "Not a creature."
        if filter_tapped and not perm.get("tapped"):
            return "Creature must be tapped."
        if perm.get("hexproof_vs_opponents"):
            return "Creature has hexproof."
        return None

    def _validate_destroy_permanent(self, iid: str, types: tuple) -> str | None:
        found = self.find_permanent(iid)
        if not found:
            return "Permanent not found."
        _, perm = found
        card = get_card(perm["card_id"])
        if card.get("card_type") not in types and not (card.get("card_type") == "Artifact Creature" and "Artifact" in types):
            return "Invalid permanent type for target."
        return None

    def _validate_gy_creature(self, card_id: str, pid: str) -> str | None:
        if card_id not in self.ctx.game_data[pid]["graveyard"]:
            return "Creature not in graveyard."
        if not is_creature(card_id):
            return "Not a creature card."
        return None

    def _check_targeted(self, target: str):
        found = self.find_permanent(target)
        if not found:
            return
        _, perm = found
        if "phantasmal" in perm.get("keywords", keywords_for(perm["card_id"])):
            owner, _ = found
            self.ctx.game_data[owner]["battlefield"].remove(perm)
            self.ctx.game_data[owner]["graveyard"].append(perm["card_id"])

    # Stack Resolution

    def resolve_top(self):
        if not self.stack:
            return
        item = self.stack.pop()
        item_id = item["stack_item_id"]
        controller = item["controller"]
        card_id = item.get("card_id", item.get("source", ""))
        targets = item.get("targets", [])
        state_changes: list[dict] = []

        if item["item_type"] == "SPELL":
            spell = spell_effect_for(card_id)
            if spell and spell.get("kind") == "counter_unless":
                self._begin_mana_leak(item, targets, state_changes)
                return
            if spell:
                for t in targets:
                    self._check_targeted(t)
                ok = self._execute_spell(spell, controller, targets, state_changes, card_id)
                self.ctx.game_data[controller]["graveyard"].append(card_id)
                self._emit_resolve(item_id, "RESOLVED" if ok else "FIZZLE", state_changes)
            elif is_creature(card_id) or get_card(card_id).get("card_type") in ("Creature", "Artifact Creature"):
                perm = self._make_permanent(card_id)
                if "haste" in perm.get("keywords", []):
                    perm["summoning_sick"] = False
                self.ctx.game_data[controller]["battlefield"].append(perm)
                state_changes.append({"change_type": "ENTER_BATTLEFIELD", "target": perm["id"]})
                self._resolve_etb(controller, perm, targets, state_changes)
                self._emit_resolve(item_id, "RESOLVED", state_changes)
            elif is_artifact(card_id):
                perm = self._make_permanent(card_id, summoning_sickness=False)
                self.ctx.game_data[controller]["battlefield"].append(perm)
                state_changes.append({"change_type": "ENTER_BATTLEFIELD", "target": perm["id"]})
                self._emit_resolve(item_id, "RESOLVED", state_changes)
            else:
                self.ctx.game_data[controller]["graveyard"].append(card_id)
                self._emit_resolve(item_id, "FIZZLE", state_changes)
        elif item["item_type"] == "ABILITY":
            abilities = get_abilities(card_id)
            ab = abilities[item.get("ability_index", 0)]
            for t in targets:
                self._check_targeted(t)
            self._execute_ability(ab, controller, targets, state_changes, item.get("source"))
            self._emit_resolve(item_id, "RESOLVED", state_changes)
        elif item["item_type"] == "TRIGGER_ABILITY":
            self._execute_trigger(item, state_changes)
            self._emit_resolve(item_id, "RESOLVED", state_changes)

        self.ctx.broadcast_game_state()
        self.check_state_based_actions()

    def _begin_mana_leak(self, leak_item, targets, state_changes):
        target_id = targets[0]
        target_item = next((s for s in self.stack if s["stack_item_id"] == target_id), None)
        if not target_item:
            self.ctx.game_data[leak_item["controller"]]["graveyard"].append(leak_item["card_id"])
            self._emit_resolve(leak_item["stack_item_id"], "FIZZLE", state_changes)
            self.ctx.broadcast_game_state()
            return
        seq = self.ctx.next_seq()
        pay = spell_effect_for(leak_item["card_id"]).get("pay_generic", 3)
        self.pending_choice = {
            "kind": "mana_leak",
            "seq_num": seq,
            "player_id": target_item["controller"],
            "leak_item": leak_item,
            "target_item": target_item,
            "pay": pay,
        }
        self.ctx.send_to(target_item["controller"], {
            "type": "TRIGGER_CHOICE",
            "seq_num": seq,
            "trigger_id": self._new_trigger_id(),
            "source_id": leak_item["card_id"],
            "effect_summary": f"Pay {{{pay}}} or {target_item['card_id']} is countered.",
            "requires_target": False,
            "legal_targets": [],
        })

    def _counter_spell(self, target_item, counter_item, state_changes):
        for i, s in enumerate(self.stack):
            if s["stack_item_id"] == target_item["stack_item_id"]:
                removed = self.stack.pop(i)
                self.ctx.game_data[removed["controller"]]["graveyard"].append(removed["card_id"])
                if isinstance(state_changes, list):
                    state_changes.append({"change_type": "COUNTER", "target": removed["stack_item_id"]})
                break

    def _execute_spell(self, spell: dict, controller: str, targets: list, changes: list, card_id: str) -> bool:
        kind = spell.get("kind")
        if kind == "damage":
            self._apply_damage(targets[0], spell["amount"], changes)
            return True
        if kind == "counter":
            self._counter_spell(
                next(s for s in self.stack if s["stack_item_id"] == targets[0]),
                {"card_id": card_id}, changes,
            )
            return True
        if kind == "exile_creature":
            self._exile_creature(targets[0], controller, spell, changes)
            return True
        if kind == "bounce_creature":
            self._bounce_creature(targets[0], changes)
            return True
        if kind == "destroy_creature":
            self._destroy_creature(targets[0], spell.get("filter"), changes)
            return True
        if kind == "destroy_permanent":
            self._destroy_permanent(targets[0], changes)
            return True
        if kind == "pump":
            found = self.find_permanent(targets[0])
            if found:
                _, perm = found
                perm["pump_power"] = perm.get("pump_power", 0) + spell["power"]
                perm["pump_toughness"] = perm.get("pump_toughness", 0) + spell["toughness"]
                changes.append({"change_type": "PUMP", "target": targets[0], "amount": spell["power"]})
            return True
        if kind == "hexproof_opponents":
            found = self.find_permanent(targets[0])
            if found:
                found[1]["hexproof_vs_opponents"] = True
            return True
        if kind == "ponder":
            pdata = self.ctx.game_data[controller]
            if pdata["library"]:
                pdata["hand"].append(pdata["library"].pop(0))
                changes.append({"change_type": "DRAW", "target": controller, "amount": 1})
            return True
        if kind == "fetch_basic_land":
            self._fetch_basic_land(controller, changes, tapped=True)
            return True
        if kind == "return_creature_gy_to_hand":
            gy = self.ctx.game_data[controller]["graveyard"]
            if targets[0] in gy:
                gy.remove(targets[0])
                self.ctx.game_data[controller]["hand"].append(targets[0])
            return True
        if kind == "discard":
            self._discard_from_hand(targets[0], spell.get("count", 2), changes)
            return True
        if kind == "ritual_mana":
            pool = self.ctx.game_data[controller].setdefault("mana_pool", {})
            for c, n in spell.get("pool", {}).items():
                pool[c] = pool.get(c, 0) + n
            return True
        if kind == "gain_life":
            self.ctx.game_data[targets[0]]["life"] += spell.get("amount", 3)
            changes.append({"change_type": "LIFE_GAIN", "target": targets[0], "amount": spell.get("amount", 3)})
            return True
        if kind == "aura":
            return self._resolve_aura(controller, card_id, targets[0], changes)
        return False

    def _execute_ability(self, ab: dict, controller: str, targets: list, changes: list, source_id: str | None):
        eff = ab.get("effect")
        if eff == "damage":
            self._apply_damage(targets[0], ab["amount"], changes)
        elif eff == "add_mana":
            pool = self.ctx.game_data[controller].setdefault("mana_pool", {})
            c = ab.get("color", "C")
            pool[c] = pool.get(c, 0) + ab.get("amount", 1)
        elif eff == "loot":
            pdata = self.ctx.game_data[controller]
            if pdata["library"]:
                pdata["hand"].append(pdata["library"].pop(0))
            if pdata["hand"]:
                discarded = pdata["hand"].pop()
                pdata["graveyard"].append(discarded)
        elif eff == "mill":
            self._mill(targets[0], ab.get("amount", 2), changes)
        elif eff == "destroy_creature":
            self._destroy_creature(targets[0], ab.get("filter"), changes)
        elif eff == "protection":
            found = self.find_permanent(targets[0])
            if found:
                found[1]["protection_color"] = "any"

    def _execute_trigger(self, item: dict, changes: list):
        trigger = item.get("trigger", {})
        controller = item["controller"]
        targets = item.get("targets", [])
        if trigger.get("kind") == "gray_merchant_etb":
            black = sum(1 for c in self.ctx.game_data[controller]["battlefield"]
                       if get_card(c["card_id"]) and get_card(c["card_id"]).get("color") == "B")
            opp = self.ctx.get_opponent(controller)
            self.ctx.game_data[opp]["life"] -= black
            self.ctx.game_data[controller]["life"] += black
            changes.append({"change_type": "DAMAGE", "target": opp, "amount": black})
            changes.append({"change_type": "LIFE_GAIN", "target": controller, "amount": black})
        elif trigger.get("kind") == "gravedigger_etb":
            if targets and targets[0] in self.ctx.game_data[controller]["graveyard"]:
                self.ctx.game_data[controller]["graveyard"].remove(targets[0])
                self.ctx.game_data[controller]["hand"].append(targets[0])

    def _resolve_etb(self, controller: str, perm: dict, targets: list, changes: list):
        etb = etb_for(perm["card_id"])
        if not etb:
            return
        self._execute_trigger({
            "controller": controller,
            "targets": targets,
            "trigger": etb,
        }, changes)

    def _resolve_aura(self, controller: str, card_id: str, creature_id: str, changes: list) -> bool:
        found = self.find_permanent(creature_id)
        if not found:
            return False
        _, perm = found
        perm.setdefault("auras", []).append({"card_id": card_id, "effect": "pacifism"})
        return True

    def _apply_damage(self, target: str, amount: int, changes: list):
        if target in self.ctx.get_pids():
            self.ctx.game_data[target]["life"] -= amount
            changes.append({"change_type": "DAMAGE", "target": target, "amount": amount})
        else:
            found = self.find_permanent(target)
            if found:
                owner, perm = found
                card = get_card(perm["card_id"])
                tough = (card.get("toughness") or 0) + perm.get("pump_toughness", 0)
                perm["damage"] = perm.get("damage", 0) + amount
                changes.append({"change_type": "DAMAGE", "target": target, "amount": amount})
                if perm["damage"] >= tough and tough > 0:
                    self.ctx.game_data[owner]["battlefield"].remove(perm)
                    self.ctx.game_data[owner]["graveyard"].append(perm["card_id"])
                    changes.append({"change_type": "DESTROY", "target": target})

    def _exile_creature(self, iid: str, controller: str, spell: dict, changes: list):
        found = self.find_permanent(iid)
        if not found:
            return
        owner, perm = found
        card = get_card(perm["card_id"])
        self.ctx.game_data[owner]["battlefield"].remove(perm)
        self.ctx.game_data[owner].setdefault("exiled", []).append(perm["card_id"])
        changes.append({"change_type": "EXILE", "target": iid})
        if spell.get("gain_life_power"):
            gain = card.get("power") or 0
            self.ctx.game_data[owner]["life"] += gain
            changes.append({"change_type": "LIFE_GAIN", "target": owner, "amount": gain})
        if spell.get("fetch_land"):
            self._fetch_basic_land(owner, changes, tapped=True)

    def _bounce_creature(self, iid: str, changes: list):
        found = self.find_permanent(iid)
        if not found:
            return
        owner, perm = found
        self.ctx.game_data[owner]["battlefield"].remove(perm)
        self.ctx.game_data[owner]["hand"].append(perm["card_id"])
        changes.append({"change_type": "BOUNCE", "target": iid})

    def _destroy_creature(self, iid: str, filt: str | None, changes: list):
        found = self.find_permanent(iid)
        if not found:
            return
        owner, perm = found
        card = get_card(perm["card_id"])
        color = card.get("color", "")
        if filt == "nonblack" and color == "B":
            return
        if filt == "nonblack_nonartifact" and (color == "B" or card.get("card_type") == "Artifact Creature"):
            return
        if filt == "tapped" and not perm.get("tapped"):
            return
        self.ctx.game_data[owner]["battlefield"].remove(perm)
        self.ctx.game_data[owner]["graveyard"].append(perm["card_id"])
        changes.append({"change_type": "DESTROY", "target": iid})

    def _destroy_permanent(self, iid: str, changes: list):
        found = self.find_permanent(iid)
        if not found:
            return
        owner, perm = found
        self.ctx.game_data[owner]["battlefield"].remove(perm)
        self.ctx.game_data[owner]["graveyard"].append(perm["card_id"])
        changes.append({"change_type": "DESTROY", "target": iid})

    def _fetch_basic_land(self, pid: str, changes: list, tapped: bool = False):
        lib = self.ctx.game_data[pid]["library"]
        land_id = find_basic_land_in_library(lib)
        if land_id:
            lib.remove(land_id)
            perm = self._make_permanent(land_id, summoning_sickness=False)
            perm["tapped"] = tapped
            self.ctx.game_data[pid]["battlefield"].append(perm)
            changes.append({"change_type": "ENTER_BATTLEFIELD", "target": perm["id"]})

    def _mill(self, pid: str, count: int, changes: list):
        lib = self.ctx.game_data[pid]["library"]
        for _ in range(min(count, len(lib))):
            c = lib.pop(0)
            self.ctx.game_data[pid]["graveyard"].append(c)

    def _discard_from_hand(self, pid: str, count: int, changes: list):
        hand = self.ctx.game_data[pid]["hand"]
        for _ in range(min(count, len(hand))):
            c = hand.pop()
            self.ctx.game_data[pid]["graveyard"].append(c)

    def _emit_resolve(self, item_id: str, result: str, state_changes: list):
        self.ctx.broadcast(stack_resolve(item_id, result, state_changes, self.ctx.next_seq()))

    def reset(self):
        self.stack.clear()
        self.priority_player = None
        self.priority_seq = 0
        self.passes_since_action = 0
        self.land_played_this_turn.clear()
        self.pending_choice = None
        self.combat_attackers.clear()
        self.combat_blockers.clear()
        self.damage_orders.clear()