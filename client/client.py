import json
import os
import socket
import threading

from models.protocol import send_pdu, receive_pdu
from models.pdu import (
    player_ready, mulligan_choice, cast_spell, priotity_pass,
    play_land, activate_ability, trigger_choice_response,
    declare_attackers, declare_blockers, assign_damage_order,
)
from models.cards import get_card, card_base_id, is_land
from models.card_effects import abilities_for

HOST = "127.0.0.1"
PORT = 4444

seq_num = 1
last_hand_seq = 0
priority_seq = 0
has_priority = False
priority_holder = None
my_hand = []
my_battlefield = []
game_stack = []
mulligan_count = 0
my_id = None
current_phase = None
active_player = None
opponent_id = None
pending_choice_seq = 0
pending_choice_id = None
lock = threading.Lock()


def _extract_my_hand(state: dict, pid: str | None) -> list[str]:
    hand = state.get("hand", [])
    if isinstance(hand, dict):
        if pid and pid in hand:
            return list(hand.get(pid, []))
        return []
    if isinstance(hand, list):
        return list(hand)
    return []


def _extract_my_battlefield(state: dict, pid: str | None) -> list[dict]:
    battlefield = state.get("battlefield", {})
    if isinstance(battlefield, dict):
        if pid and pid in battlefield:
            return list(battlefield.get(pid, []))
        return []
    if isinstance(battlefield, list):
        return list(battlefield)
    return []


def _card_display_name(card_id: str) -> str:
    card = get_card(card_id)
    return card["card_name"] if card else card_id


def _print_hand_summary(hand_cards):
    if not hand_cards:
        print("  Hand: (empty)")
        return
    print("  Hand:")
    for card_id in hand_cards:
        print(f"    - {_card_display_name(card_id)}")


def _print_battlefield_summary(state: dict | None = None):
    if state is None:
        state = {}
    battlefield_map = state.get("battlefield", {})
    if not isinstance(battlefield_map, dict) or not battlefield_map:
        print("  Battlefield summary: (empty)")
        return

    print("  Battlefield summary:")
    for pid, perms in battlefield_map.items():
        label = "You" if pid == my_id else pid
        summary_items = []
        for perm in perms:
            card_id = perm.get("card_id") or perm.get("source") or perm.get("id") or "?"
            name = _card_display_name(card_id) if card_id != "?" else "unknown"
            if perm.get("tapped"):
                name += " [tapped]"
            if perm.get("damage"):
                name += f" (damage {perm.get('damage')})"
            summary_items.append(name)
        print(f"    {label}: {', '.join(summary_items) if summary_items else '(empty)'}")


def _print_command_examples():
    print("Examples:")
    print("  play mountain_001")
    print("  play mountain")
    print("  land mountain_001")
    print("  play lightning_bolt_001")
    print("  cast lightning_bolt_001")
    print("  tap mountain_001")


def _print_turn_status(state: dict | None = None):
    if state is None:
        state = {}
    phase = state.get("phase") or current_phase or "UNKNOWN"
    turn = state.get("turn")
    active = state.get("active_player") or active_player or "?"
    prio = state.get("priority_player") or state.get("priority_holder") or priority_holder
    if prio is None:
        prio = my_id if has_priority else None

    print("  ── Turn status ──")
    if turn is not None:
        print(f"  Turn: {turn}")
    print(f"  Current phase: {phase}")
    print(f"  Active player: {active}")
    if prio:
        print(f"  Priority: {prio}" + (" (YOU)" if prio == my_id else ""))
    else:
        print("  Priority: waiting for next grant")
    _print_battlefield_summary(state or {"battlefield": {my_id: my_battlefield} if my_id else {}})


def _resolve_card_id(token: str, hand_cards: list[str]) -> str | None:
    if not token:
        return None
    token_norm = token.strip().lower()
    if token_norm in {c.lower() for c in hand_cards}:
        return next(c for c in hand_cards if c.lower() == token_norm)
    if token_norm in {card_base_id(c).lower() for c in hand_cards}:
        return next(c for c in hand_cards if card_base_id(c).lower() == token_norm)
    for card_id in hand_cards:
        card = get_card(card_id)
        if not card:
            continue
        names = {card.get("card_name", "").strip().lower(), card.get("card_name", "").strip().lower().replace(" ", "_")}
        if token_norm in names:
            return card_id
    return None

# ── Receiver thread ───────────────────────────────────────────────────────────

def receiver(sock):
    global last_hand_seq, my_hand, priority_seq, has_priority, priority_holder
    global my_battlefield, game_stack, current_phase, active_player, opponent_id
    global pending_choice_seq, pending_choice_id

    while True:
        msg = receive_pdu(sock)
        if msg is None:
            print("\n[DISCONNECTED] Server closed connection.")
            break

        msg_type = msg.get("type")
        seq = msg.get("seq_num", 0)

        if msg_type == "GAME_STATE_UPDATE" and msg.get("state", {}).get("phase") == "MULLIGAN":
            with lock:
                last_hand_seq = seq
                my_hand = _extract_my_hand(msg.get("state", {}), my_id)

        print(f"\n── {msg_type} (seq={seq}) ──")

        if msg_type == "GAME_STATE_UPDATE":
            state = msg.get("state", {})
            phase = state.get("phase")
            with lock:
                current_phase = phase
                active_player = state.get("active_player")
                my_hand = _extract_my_hand(state, my_id)
                my_battlefield = _extract_my_battlefield(state, my_id)
                game_stack = state.get("stack", [])
                life = state.get("life_totals", {})
                if my_id and life:
                    opponent_id = next((p for p in life if p != my_id), None)

            print(f"  Phase: {phase}")
            _print_turn_status(state)

            if phase == "LOBBY":
                print(f"  Players ready: {state.get('players_ready')}/2")
                w = state.get("waiting_for", [])
                if w:
                    print(f"  Waiting for:   {w}")

            elif phase == "MULLIGAN":
                print(f"  Life totals:   {state.get('life_totals')}")
                print(f"  Your hand:     {state.get('hand')}")
                print(f"  Opponent hand: {state.get('hand_counts')}")
                print(f"  Library sizes: {state.get('library_counts')}")
                print()
                print("  Commands: keep | mulligan | bottom <card1> <card2> ...")

            else:
                print(f"  Turn:          {state.get('turn')}")
                print(f"  Active player: {state.get('active_player')}")
                print(f"  Life totals:   {state.get('life_totals')}")
                print(f"  Opponent hand: {state.get('hand_counts')}")
                print(f"  Stack:         {state.get('stack')}")
                prio = state.get("priority_player")
                if prio:
                    print(f"  Priority:      {prio}" + (" (YOU)" if prio == my_id else ""))
                _print_hand_summary(my_hand)
                _print_battlefield_summary(state)
                print()
                print("  Commands: pass | play <card> [target] | land <card> | cast <card> [target] | tap <perm_id> [target] | concede")

        elif msg_type == "PRIORITY_GRANT":
            grantee = msg.get("player_id")
            with lock:
                priority_seq = seq
                has_priority = (grantee == my_id)
                priority_holder = grantee
            print(f"  Priority granted to: {grantee}" + (" (YOU — respond!)" if grantee == my_id else ""))
            print(f"  Time limit: {msg.get('time_limit_ms')}ms")
            _print_turn_status({
                "phase": current_phase,
                "active_player": active_player,
                "priority_player": grantee,
                "turn": None,
            })
            if grantee == my_id:
                _print_hand_summary(my_hand)
                print("  → pass | play <card> [target] | land <card> | cast <card> [target] | tap <perm_id> [target]")

        elif msg_type == "STACK_PUSH":
            print(f"  Stack +{msg.get('item_type')}: {msg.get('source')} "
                  f"(id={msg.get('stack_item_id')}, ctrl={msg.get('controller')})")
            print(f"  Targets: {msg.get('targets')}")

        elif msg_type == "STACK_RESOLVE":
            print(f"  Resolved {msg.get('stack_item_id')}: {msg.get('result')}")
            changes = msg.get("state_changes", {})
            if changes:
                print(f"  Changes: {json.dumps(changes)}")

        elif msg_type == "TRIGGER_CHOICE":
            with lock:
                pending_choice_seq = seq
                pending_choice_id = msg.get("trigger_id")
            print(f"  Choice required: {msg.get('effect_summary')}")
            print("  → yes (accept/pay) | no (decline)")

        elif msg_type == "PHASE_TRANSITION":
            print(f"  {msg.get('from_phase')} → {msg.get('to_phase')}")
            print(f"  Active player: {msg.get('active_player')}  Turn: {msg.get('turn')}")

        elif msg_type == "GAME_OVER":
            print(f"  GAME OVER!")
            print(f"  Winner: {msg.get('winner_id')}")
            print(f"  Loser:  {msg.get('loser_id')}")
            print(f"  Reason: {msg.get('reason')}")
            print()
            print("  Send PLAYER_READY to start a new game.")

        elif msg_type == "ERROR":
            print(f"  [!] {msg.get('code')}: {msg.get('message')}")

        else:
            print(f"  {msg}")

# ── Command helpers ─────────────────────────────────────────────────────────────

def _mana_payment_for_card(card_id: str) -> dict:
    """Build a simple mana_payment dict from the card's mana_cost."""
    card = get_card(card_id)
    if not card:
        return {}
    cost = card.get("mana_cost", {})
    payment = {}
    for k, v in cost.items():
        payment[k] = int(v)
    return payment

def _print_battlefield():
    if not my_battlefield:
        print("  (empty battlefield)")
        return
    for p in my_battlefield:
        tapped = " [TAPPED]" if p.get("tapped") else ""
        card_id = p.get("card_id") or p.get("id") or p.get("instance_id") or ""
        card = get_card(card_id) if card_id else None
        name = card["card_name"] if card else card_id
        perm_id = p.get("instance_id") or p.get("id") or ""
        print(f"    {perm_id}: {name}{tapped}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global seq_num, mulligan_count, my_id

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect((HOST, PORT))
    print(f"Connected to {HOST}:{PORT}")

    threading.Thread(target=receiver, args=(client,), daemon=True).start()

    my_id = input("Player ID: ").strip()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    deck1_path = os.path.join(root, "jason files", "deck1.json")
    deck2_path = os.path.join(root, "jason files", "deck2.json")

    print("Deck options: 1=deck1.json (red burn) | 2=deck2.json (UW control) | c=custom | d=default")
    deck_choice = input("Deck [1]: ").strip().lower() or "1"

    if deck_choice == "1":
        with open(deck1_path, encoding="utf-8") as f:
            deck = json.load(f)["deck_list"]
        print(f"Loaded deck1 ({len(deck)} cards)")
    elif deck_choice == "2":
        with open(deck2_path, encoding="utf-8") as f:
            deck = json.load(f)["deck_list"]
        print(f"Loaded deck2 ({len(deck)} cards)")
    elif deck_choice == "c":
        print("Enter card IDs one per line. Blank line to finish:")
        deck = []
        while True:
            c = input("  ").strip()
            if not c:
                break
            deck.append(c)
    else:
        deck = [
            "lightning_bolt_001", "lightning_bolt_002", "lightning_bolt_003",
            "shock_001", "shock_002",
            "mountain_001", "mountain_002", "mountain_003", "mountain_004",
            "mountain_005", "mountain_006", "mountain_007",
        ]
        print(f"Using default deck ({len(deck)} cards)")

    send_pdu(client, player_ready(my_id, deck, seq_num))
    seq_num += 1
    print("[SENT] PLAYER_READY")

    print("\nWaiting for game to start...")
    print("Commands: keep | mulligan | bottom <cards...> | pass | play <card> [target] | "
          "land <card> | cast <card> [target] | tap <perm_id> [target] | attack <creature>... | "
          "block <blocker> <attacker>... | order <attacker> <blocker>... | yes | no | concede | quit")
    _print_command_examples()

    while True:
        print("\n--------------------")
        _print_turn_status({
            "phase": current_phase,
            "active_player": active_player,
            "priority_player": my_id if has_priority else opponent_id,
            "turn": None,
        })
        try:
            cmd = input("Command> ").strip()
        except EOFError:
            break

        if not cmd:
            continue

        parts = cmd.split()
        action = parts[0].lower()

        with lock:
            echo = last_hand_seq
            hand = list(my_hand)
            mc = mulligan_count
            pseq = priority_seq
            bf = list(my_battlefield)
            opp = opponent_id

        # ── MULLIGAN commands ──────────────────────────────────────
        if action == "keep" and len(parts) == 1:
            if mc > 0:
                print(f"  You mulliganed {mc} time(s). Use: bottom <{mc} card(s)>")
                print(f"  Your hand: {hand}")
                continue
            send_pdu(client, mulligan_choice(True, [], echo))
            print(f"[SENT] MULLIGAN_CHOICE keep=True, cards_to_bottom=[]")

        elif action == "bottom":
            cards = parts[1:]
            if len(cards) != mc:
                print(f"  Need exactly {mc} card(s) to bottom. Got {len(cards)}.")
                continue
            send_pdu(client, mulligan_choice(True, cards, echo))
            mulligan_count = 0
            print(f"[SENT] MULLIGAN_CHOICE keep=True, cards_to_bottom={cards}")

        elif action == "mulligan":
            send_pdu(client, mulligan_choice(False, [], echo))
            mulligan_count += 1
            print(f"[SENT] MULLIGAN_CHOICE keep=False (mulligan #{mulligan_count})")

        # ── PRIORITY_PASS ───────────────────────────────────────────
        elif action == "pass":
            if not has_priority:
                print("  You do not currently hold priority; wait for the server to grant it.")
                continue
            send_pdu(client, priotity_pass(pseq))
            print(f"[SENT] PRIORITY_PASS seq={pseq}")

        # ── PLAY shorthand ─────────────────────────────────────────
        elif action == "play":
            if len(parts) < 2:
                print("  Usage: play <card_id> [target]")
                _print_command_examples()
                continue
            card_id = _resolve_card_id(parts[1], hand)
            targets = parts[2:] if len(parts) > 2 else []
            if card_id is None:
                print(f"  '{parts[1]}' is not in your hand. Try one of: {hand}")
                _print_command_examples()
                continue
            if not has_priority:
                print("  You do not currently hold priority; wait for the server to grant it.")
                continue
            if is_land(card_id):
                send_pdu(client, play_land(card_id, pseq))
                print(f"[SENT] PLAY_LAND {card_id} seq={pseq}")
            else:
                payment = _mana_payment_for_card(card_id)
                send_pdu(client, cast_spell(card_id, targets, payment, pseq))
                print(f"[SENT] CAST_SPELL {card_id} targets={targets} mana={payment} seq={pseq}")

        # ── PLAY_LAND ───────────────────────────────────────────────
        elif action == "land":
            if len(parts) != 2:
                print("  Usage: land <card_id>")
                _print_command_examples()
                continue
            card_id = _resolve_card_id(parts[1], hand)
            if card_id is None:
                print(f"  '{parts[1]}' is not in your hand. Try one of: {hand}")
                _print_command_examples()
                continue
            if not has_priority:
                print("  You do not currently hold priority; wait for the server to grant it.")
                continue
            send_pdu(client, play_land(card_id, pseq))
            print(f"[SENT] PLAY_LAND {card_id} seq={pseq}")

        # ── CAST_SPELL ──────────────────────────────────────────────
        elif action == "cast":
            if len(parts) < 2:
                print("  Usage: cast <card_id> [target]")
                print("  Target = opponent player_id, perm instance_id, or stack_item_id for counters")
                _print_command_examples()
                continue
            card_id = _resolve_card_id(parts[1], hand)
            if card_id is None:
                print(f"  '{parts[1]}' is not in your hand. Try one of: {hand}")
                _print_command_examples()
                continue
            targets = parts[2:] if len(parts) > 2 else []
            if not has_priority:
                print("  You do not currently hold priority; wait for the server to grant it.")
                continue
            payment = _mana_payment_for_card(card_id)
            send_pdu(client, cast_spell(card_id, targets, payment, pseq))
            print(f"[SENT] CAST_SPELL {card_id} targets={targets} mana={payment} seq={pseq}")

        # ── ACTIVATE_ABILITY (tap land/creature for mana or damage) ───
        elif action == "tap":
            if len(parts) < 2:
                print("  Usage: tap <perm_instance_id> [target]")
                _print_battlefield()
                continue
            perm_id = parts[1]
            targets = parts[2:] if len(parts) > 2 else []
            perm = next((p for p in bf if p["instance_id"] == perm_id), None)
            if not perm:
                print(f"  Permanent '{perm_id}' not on your battlefield.")
                _print_battlefield()
                continue
            base = card_base_id(perm["card_id"])
            mana_color = {"mountain": "R", "forest": "G", "island": "U", "plains": "W", "swamp": "B",
                          "llanowar_elves": "G", "elvish_mystic": "G"}.get(base)
            cost = {"tap": True, "mana": {}}
            mana_pay = {mana_color: 1} if mana_color else {}
            if not has_priority:
                print("  You do not currently hold priority; wait for the server to grant it.")
                continue
            send_pdu(client, activate_ability(perm_id, 0, targets, pseq, cost))
            print(f"[SENT] ACTIVATE_ABILITY {perm_id} targets={targets}")

        # ── DECLARE_ATTACKERS ─────────────────────────────────────────
        elif action == "attack":
            if len(parts) < 2:
                print("  Usage: attack <creature_id> [creature_id ...]")
                continue
            attackers = [{"creature_id": cid} for cid in parts[1:]]
            send_pdu(client, declare_attackers(attackers, seq_num))
            seq_num += 1
            print(f"[SENT] DECLARE_ATTACKERS attackers={parts[1:]}")

        # ── DECLARE_BLOCKERS ─────────────────────────────────────────
        elif action == "block":
            if len(parts) < 3 or len(parts[1:]) % 2 != 0:
                print("  Usage: block <blocker_id> <attacker_id> [<blocker_id> <attacker_id> ...]")
                continue
            blockers = []
            for i in range(1, len(parts), 2):
                blockers.append({"creature_id": parts[i], "blocking_id": parts[i+1]})
            send_pdu(client, declare_blockers(blockers, seq_num))
            seq_num += 1
            print(f"[SENT] DECLARE_BLOCKERS blockers={blockers}")

        # ── ASSIGN_DAMAGE_ORDER ────────────────────────────────────────
        elif action == "order":
            if len(parts) < 3:
                print("  Usage: order <attacker_id> <blocker_id> [blocker_id ...]")
                continue
            attacker_id = parts[1]
            blocker_order = parts[2:]
            send_pdu(client, assign_damage_order(attacker_id, blocker_order, seq_num))
            seq_num += 1
            print(f"[SENT] ASSIGN_DAMAGE_ORDER attacker={attacker_id} order={blocker_order}")

        # ── CONCEDE ─────────────────────────────────────────────────
        elif action == "concede":
            send_pdu(client, {
                "type": "CONCEDE",
                "seq_num": seq_num,
                "player_id": my_id,
            })
            seq_num += 1
            print("[SENT] CONCEDE")

        elif action == "bf" or action == "battlefield":
            _print_battlefield()

        elif action == "hand":
            print(f"  Hand: {hand}")

        # ── QUIT ────────────────────────────────────────────────────
        elif action == "quit":
            break

        else:
            print("Commands: keep | mulligan | bottom <cards...> | pass | play <card> [target] | "
                  "land <card> | cast <card> [target] | tap <perm_id> [target] | attack <creature>... | "
                  "block <blocker> <attacker>... | order <attacker> <blocker>... | bf | hand | concede | quit")
            _print_command_examples()

    client.close()

if __name__ == "__main__":
    main()
