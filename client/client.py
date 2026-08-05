import json
import os
import socket
import threading

from models.protocol import send_pdu, receive_pdu
from models.pdu import (
    player_ready, mulligan_choice, cast_spell, priotity_pass,
    play_land, activate_ability, trigger_choice_response,
)
from models.cards import get_card, card_base_id
from models.card_effects import abilities_for

HOST = "127.0.0.1"
PORT = 4444

seq_num = 1
last_hand_seq = 0
priority_seq = 0
has_priority = False
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

# ── Receiver thread ───────────────────────────────────────────────────────────

def receiver(sock):
    global last_hand_seq, my_hand, priority_seq, has_priority
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
                my_hand = msg.get("state", {}).get("hand", [])

        print(f"\n── {msg_type} (seq={seq}) ──")

        if msg_type == "GAME_STATE_UPDATE":
            state = msg.get("state", {})
            phase = state.get("phase")
            with lock:
                current_phase = phase
                active_player = state.get("active_player")
                my_hand = state.get("hand", my_hand)
                my_battlefield = state.get("battlefield", {}).get(my_id, [])
                game_stack = state.get("stack", [])
                life = state.get("life_totals", {})
                if my_id and life:
                    opponent_id = next((p for p in life if p != my_id), None)

            print(f"  Phase: {phase}")

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
                print(f"  Your hand:     {state.get('hand')}")
                print(f"  Opponent hand: {state.get('hand_counts')}")
                print(f"  Battlefield:   {state.get('battlefield')}")
                print(f"  Stack:         {state.get('stack')}")
                prio = state.get("priority_player")
                if prio:
                    print(f"  Priority:      {prio}" + (" (YOU)" if prio == my_id else ""))
                print()
                print("  Commands: pass | land <card> | cast <card> <target> | tap <perm_id> [target] | concede")

        elif msg_type == "PRIORITY_GRANT":
            grantee = msg.get("player_id")
            with lock:
                priority_seq = seq
                has_priority = (grantee == my_id)
            print(f"  Priority granted to: {grantee}" + (" (YOU — respond!)" if grantee == my_id else ""))
            print(f"  Time limit: {msg.get('time_limit_ms')}ms")
            if grantee == my_id:
                print("  → pass | land <card> | cast <card> <target> | tap <perm_id> [target]")

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
        card = get_card(p["card_id"])
        name = card["card_name"] if card else p["card_id"]
        print(f"    {p['instance_id']}: {name}{tapped}")

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
    print("Commands: keep | mulligan | bottom <cards...> | pass | land <card> | "
          "cast <card> [target] | tap <perm_id> [target] | yes | no | concede | quit")

    while True:
        try:
            cmd = input().strip()
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
            send_pdu(client, priotity_pass(pseq))
            print(f"[SENT] PRIORITY_PASS seq={pseq}")

        # ── PLAY_LAND ───────────────────────────────────────────────
        elif action == "land":
            if len(parts) != 2:
                print("  Usage: land <card_id>")
                continue
            card_id = parts[1]
            send_pdu(client, play_land(card_id, seq_num))
            seq_num += 1
            print(f"[SENT] PLAY_LAND {card_id}")

        # ── CAST_SPELL ──────────────────────────────────────────────
        elif action == "cast":
            if len(parts) < 2:
                print("  Usage: cast <card_id> [target]")
                print("  Target = opponent player_id, perm instance_id, or stack_item_id for counters")
                continue
            card_id = parts[1]
            targets = parts[2:] if len(parts) > 2 else []
            payment = _mana_payment_for_card(card_id)
            send_pdu(client, cast_spell(card_id, targets, payment, pseq))
            print(f"[SENT] CAST_SPELL {card_id} targets={targets} mana={payment}")

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
            send_pdu(client, activate_ability(perm_id, 0, targets, pseq, cost))
            print(f"[SENT] ACTIVATE_ABILITY {perm_id} targets={targets}")

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
            print("Commands: keep | mulligan | bottom <cards...> | pass | land <card> | "
                  "cast <card> <target> | tap <perm_id> | bf | hand | concede | quit")

    client.close()

if __name__ == "__main__":
    main()
