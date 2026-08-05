import socket
import threading

from models.protocol import send_pdu, receive_pdu
from models.pdu import player_ready, mulligan_choice, pong

HOST = "127.0.0.1"
PORT = 4444

# Client state
seq_num = 1                  # PLAYER_READY counter
last_hand_seq = 0            # seq of the last GAME_STATE_UPDATE that showed our hand (MULLIGAN)
my_hand = []
mulligan_count = 0
my_id = None                 # player ID (set after PLAYER_READY)
lock = threading.Lock()

# ── Receiver thread ───────────────────────────────────────────────────────────

def receiver(sock):
    global last_hand_seq, my_hand

    while True:
        msg = receive_pdu(sock)
        if msg is None:
            print("\n[DISCONNECTED] Server closed connection.")
            break

        msg_type = msg.get("type")
        seq = msg.get("seq_num", 0)

        # Update hand-state sequence on GAME_STATE_UPDATE during MULLIGAN
        if msg_type == "GAME_STATE_UPDATE" and msg.get("state", {}).get("phase") == "MULLIGAN":
            with lock:
                last_hand_seq = seq
                my_hand = msg.get("state", {}).get("hand", [])

        print(f"\n── {msg_type} (seq={seq}) ──")

        if msg_type == "GAME_STATE_UPDATE":
            state = msg.get("state", {})
            phase = state.get("phase")
            print(f"  Phase: {phase}")

            if phase == "LOBBY":
                print(f"  Players ready: {state.get('players_ready')}/2")
                w = state.get("waiting_for", [])
                if w:
                    print(f"  Waiting for:   {w}")

            elif phase == "MULLIGAN":
                hand = state.get("hand", [])
                with lock:
                    my_hand = hand
                print(f"  Life totals:   {state.get('life_totals')}")
                print(f"  Your hand:     {hand}")
                print(f"  Opponent hand: {state.get('hand_counts')}")
                print(f"  Library sizes: {state.get('library_counts')}")
                print()
                print("  Commands: keep | mulligan | bottom <card1> <card2> ...")

            elif phase in ["UNTAP", "UPKEEP", "DRAW", "PRECOMBAT_MAIN", "POSTCOMBAT_MAIN",
                           "BEGIN_COMBAT", "DECLARE_ATTACKERS", "DECLARE_BLOCKERS",
                           "ASSIGN_DAMAGE_ORDER", "FIRST_STRIKE_DAMAGE", "COMBAT_DAMAGE",
                           "END_OF_COMBAT", "END_STEP", "CLEANUP"]:
                print(f"  Turn:          {state.get('turn')}")
                print(f"  Active player: {state.get('active_player')}")
                print(f"  Life totals:   {state.get('life_totals')}")
                print(f"  Your hand:     {state.get('hand')}")
                print(f"  Opponent hand: {state.get('hand_counts')}")
                print(f"  Library sizes: {state.get('library_counts')}")
                print(f"  Battlefield:   {state.get('battlefield')}")
                print(f"  Graveyard:     {state.get('graveyard')}")
                print()
                print("  Commands: concede")

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

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global seq_num, mulligan_count, my_id

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect((HOST, PORT))
    print(f"Connected to {HOST}:{PORT}")

    threading.Thread(target=receiver, args=(client,), daemon=True).start()

    # Step 1: send PLAYER_READY
    my_id = input("Player ID: ").strip()

    # Default test deck
    deck = [
        "lightning_bolt_001", "lightning_bolt_002", "lightning_bolt_003",
        "shock_001", "shock_002",
        "mountain_001", "mountain_002", "mountain_003",
    ]
    print(f"Using default deck: {deck}")
    use_custom = input("Use custom deck? (y/n): ").strip().lower()
    if use_custom == "y":
        print("Enter card IDs one per line. Blank line to finish:")
        deck = []
        while True:
            c = input("  ").strip()
            if not c:
                break
            deck.append(c)

    send_pdu(client, player_ready(my_id, deck, seq_num))
    seq_num += 1
    print("[SENT] PLAYER_READY")

    # Step 2: command loop
    print("\nWaiting for game to start...")
    print("Commands: keep | mulligan | bottom <cards...> | concede | quit")

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

        # ── CONCEDE ─────────────────────────────────────────────────
        elif action == "concede":
            send_pdu(client, {
                "type": "CONCEDE",
                "seq_num": seq_num,
                "player_id": my_id,
            })
            seq_num += 1
            print("[SENT] CONCEDE")

        # ── QUIT ────────────────────────────────────────────────────
        elif action == "quit":
            break

        else:
            print("Commands: keep | mulligan | bottom <cards...> | concede | quit")

    client.close()

if __name__ == "__main__":
    main()
