import socket
import threading
import random

from models.protocol import receive_pdu, send_pdu
from models.pdu import game_state_update, error, pong

HOST = "127.0.0.1"
PORT = 4444

# ── Global state ──────────────────────────────────────────────────────────────
clients = []                # connected sockets (max 2)
players = {}                # player_id -> socket
ready_data = {}             # player_id -> {"deck": [...]}
game_data = {}              # player_id -> {"life", "hand", "library", "mulligan_count", "battlefield", "graveyard"}
mulligan_kept = {}          # player_id -> True once they keep
last_state_seq = {}         # player_id -> seq_num of the last GAME_STATE_UPDATE sent to them (hand state)

# Game-wide state (protected by lock)
game_state = "LOBBY"        # LOBBY | GAME_SETUP | MULLIGAN | IN_GAME | GAME_OVER
server_seq = 0
lock = threading.Lock()
mull_lock = threading.Lock()

# IN_GAME specific state
turn_number = 0
active_player = None        # player_id of the current active player
current_phase = None        # e.g., "UNTAP", "UPKEEP", ...
stack = []
game_over = False

# ── Helpers ───────────────────────────────────────────────────────────────────

def next_seq():
    global server_seq
    server_seq += 1
    return server_seq

def broadcast(msg):
    for conn in clients:
        try:
            send_pdu(conn, msg)
        except:
            pass

def send_to(player_id, msg):
    conn = players.get(player_id)
    if conn:
        try:
            send_pdu(conn, msg)
        except:
            pass

def send_error(conn, code, text, rejected, seq):
    send_pdu(conn, {
        "type": "ERROR",
        "seq_num": seq,
        "code": code,
        "message": text,
        "rejected_action": rejected,
    })

def get_pids():
    return list(game_data.keys())

def get_opponent(pid):
    pids = get_pids()
    return [p for p in pids if p != pid][0]

# ── Deck validation ───────────────────────────────────────────────────────────

LEGAL_CARDS = {
    "mountain_001", "mountain_002", "mountain_003",
    "forest_001",   "forest_002",   "forest_003",
    "island_001",   "island_002",   "island_003",
    "swamp_001",    "swamp_002",    "swamp_003",
    "plains_001",   "plains_002",   "plains_003",
    "goblin_guide_001", "goblin_guide_002",
    "grizzly_bears_001","grizzly_bears_002",
    "llanowar_elves_001","llanowar_elves_002",
    "lightning_bolt_001","lightning_bolt_002",
    "lightning_bolt_003","lightning_bolt_004",
    "shock_001","shock_002","shock_003",
    "counterspell_001","counterspell_002",
}

def validate_deck(deck):
    if not deck:
        return False, "Deck is empty; minimum is 1 card."
    if len(deck) > 50:
        return False, f"Deck has {len(deck)} cards; maximum is 50."
    bad = [c for c in deck if c not in LEGAL_CARDS]
    if bad:
        return False, f"Unknown card(s): {bad}"
    return True, None

# ── LOBBY helpers ─────────────────────────────────────────────────────────────

def lobby_update():
    waiting = [p for p in players if p not in ready_data]
    return game_state_update({
        "phase":         "LOBBY",
        "players_ready": len(ready_data),
        "waiting_for":   waiting,
    }, next_seq())

# ── Broadcast GAME_STATE_UPDATE (IN_GAME) ────────────────────────────────────

def broadcast_game_state():
    """Broadcast a personalized GAME_STATE_UPDATE to both players."""
    pids = get_pids()
    if not pids:
        return
    seq = next_seq()
    for pid in pids:
        opp = get_opponent(pid)
        state = {
            "turn": turn_number,
            "phase": current_phase or "UNTAP",
            "active_player": active_player,
            "life_totals": {p: game_data[p]["life"] for p in pids},
            "hand": game_data[pid]["hand"],
            "hand_counts": {opp: len(game_data[opp]["hand"])},
            "library_counts": {p: len(game_data[p]["library"]) for p in pids},
            "battlefield": {p: game_data[p].get("battlefield", []) for p in pids},
            "graveyard": {p: game_data[p].get("graveyard", []) for p in pids},
            "stack": stack,
        }
        send_to(pid, game_state_update(state, seq))

# ── GAME_OVER handling ────────────────────────────────────────────────────────

def trigger_game_over(reason, winner_id, loser_id):
    """Broadcast GAME_OVER and reset to LOBBY."""
    global game_state, game_over
    with lock:
        if game_over:
            return
        game_over = True
        game_state = "GAME_OVER"

    broadcast({
        "type": "GAME_OVER",
        "seq_num": next_seq(),
        "winner_id": winner_id,
        "loser_id": loser_id,
        "reason": reason,
    })
    print(f"[GAME_OVER] {reason} – Winner: {winner_id}, Loser: {loser_id}")
    reset_to_lobby()

def reset_to_lobby():
    """Reset all game state, keep TCP connections."""
    global game_state, game_data, mulligan_kept, last_state_seq
    global turn_number, current_phase, active_player, stack, game_over

    with lock:
        game_state = "LOBBY"
        game_data.clear()
        mulligan_kept.clear()
        last_state_seq.clear()
        turn_number = 0
        current_phase = None
        active_player = None
        stack = []
        game_over = False
        players.clear()
        ready_data.clear()

    print("[RESET] Returned to LOBBY state. Send new PLAYER_READY.")

# ── Check win conditions ──────────────────────────────────────────────────────

def check_win_conditions():
    """Check for LIFE_ZERO or DECK_EMPTY win conditions."""
    pids = get_pids()
    if not pids:
        return False

    for pid in pids:
        # Life total <= 0
        if game_data[pid]["life"] <= 0:
            opp = get_opponent(pid)
            trigger_game_over("LIFE_ZERO", opp, pid)
            return True

        # Library empty (would be checked on draw)
        if len(game_data[pid]["library"]) == 0:
            opp = get_opponent(pid)
            trigger_game_over("DECK_EMPTY", opp, pid)
            return True

    return False

# ── Start IN_GAME ─────────────────────────────────────────────────────────────

def start_in_game():
    """Transition from MULLIGAN to IN_GAME."""
    global game_state, turn_number, current_phase, active_player, stack, game_over

    with lock:
        game_state = "IN_GAME"
        turn_number = 1
        current_phase = "UNTAP"
        stack = []
        game_over = False

    # Broadcast transition to UNTAP
    broadcast({
        "type": "PHASE_TRANSITION",
        "seq_num": next_seq(),
        "from_phase": "MULLIGAN",
        "to_phase": "UNTAP",
        "active_player": active_player,
        "turn": turn_number,
    })

    # Broadcast initial game state
    broadcast_game_state()

    # Check win conditions (shouldn't trigger here)
    check_win_conditions()

    print(f"[IN_GAME] Started. Active player: {active_player}, Turn {turn_number}")

# ── GAME_SETUP ────────────────────────────────────────────────────────────────

def run_game_setup():
    """Setup the game: shuffle, draw 7, determine first player."""
    global game_state, last_state_seq, active_player

    with lock:
        game_state = "GAME_SETUP"

    pids = list(ready_data.keys())
    active_player = random.choice(pids)

    for pid in pids:
        deck = list(ready_data[pid]["deck"])
        random.shuffle(deck)
        game_data[pid] = {
            "life": 20,
            "hand": deck[:7],
            "library": deck[7:],
            "mulligan_count": 0,
            "battlefield": [],
            "graveyard": [],
        }

    with lock:
        game_state = "MULLIGAN"

    # Send personalized GAME_STATE_UPDATE to each player
    seq = next_seq()
    for pid in pids:
        opp = get_opponent(pid)
        state = {
            "turn": 0,
            "phase": "MULLIGAN",
            "active_player": active_player,
            "life_totals": {p: game_data[p]["life"] for p in pids},
            "hand": game_data[pid]["hand"],
            "hand_counts": {opp: len(game_data[opp]["hand"])},
            "library_counts": {p: len(game_data[p]["library"]) for p in pids},
            "battlefield": {p: [] for p in pids},
            "graveyard": {p: [] for p in pids},
            "stack": [],
        }
        last_state_seq[pid] = seq
        send_to(pid, game_state_update(state, seq))

    print(f"[GAME_SETUP] Done. Active player: {active_player}. Now in MULLIGAN.")

# ── MULLIGAN ──────────────────────────────────────────────────────────────────

def handle_mulligan(conn, msg, pid):
    """Handle MULLIGAN_CHOICE PDU."""
    global game_state

    keep = msg.get("keep", True)
    to_bot = msg.get("cards_to_bottom", [])
    msg_seq = msg.get("seq_num", 0)
    pdata = game_data[pid]
    pids = get_pids()

    # Validate seq_num
    expected_seq = last_state_seq.get(pid, 0)
    if msg_seq != expected_seq:
        send_error(conn, "STALE_ACTION",
            f"Priority token mismatch. Expected seq_num {expected_seq}, got {msg_seq}.",
            msg, msg_seq)
        return

    # Prevent double-keep
    if mulligan_kept.get(pid, False):
        send_error(conn, "ILLEGAL_ACTION",
            f"Player {pid} has already kept their hand.", msg, msg_seq)
        return

    if keep:
        # Validate cards_to_bottom count
        if len(to_bot) != pdata["mulligan_count"]:
            send_error(conn, "ILLEGAL_ACTION",
                f"cards_to_bottom must have exactly {pdata['mulligan_count']} card(s), "
                f"got {len(to_bot)}.", msg, msg_seq)
            return

        # Validate every card is in hand
        for c in to_bot:
            if c not in pdata["hand"]:
                send_error(conn, "ILLEGAL_ACTION",
                    f"Card '{c}' is not in your hand.", msg, msg_seq)
                return

        # Move cards to bottom
        for c in to_bot:
            pdata["hand"].remove(c)
            pdata["library"].append(c)

        print(f"[MULLIGAN] {pid} keeps (put {len(to_bot)} to bottom).")

        with mull_lock:
            mulligan_kept[pid] = True
            both_kept = all(mulligan_kept.get(p) for p in pids)

        if both_kept:
            start_in_game()

    else:
        # Mulligan — reshuffle and redraw 7
        pdata["mulligan_count"] += 1
        pdata["library"] = pdata["hand"] + pdata["library"]
        random.shuffle(pdata["library"])
        pdata["hand"] = pdata["library"][:7]
        pdata["library"] = pdata["library"][7:]

        print(f"[MULLIGAN] {pid} mulligans (#{pdata['mulligan_count']}).")

        # Send new hand to this player only
        new_seq = next_seq()
        opp = get_opponent(pid)
        state = {
            "turn": 0,
            "phase": "MULLIGAN",
            "active_player": active_player,
            "life_totals": {p: game_data[p]["life"] for p in pids},
            "hand": pdata["hand"],
            "hand_counts": {opp: len(game_data[opp]["hand"])},
            "library_counts": {p: len(game_data[p]["library"]) for p in pids},
            "battlefield": {p: [] for p in pids},
            "graveyard": {p: [] for p in pids},
            "stack": [],
        }
        last_state_seq[pid] = new_seq
        send_to(pid, game_state_update(state, new_seq))

# ── Handle CONCEDE ────────────────────────────────────────────────────────────

def handle_concede(conn, msg, pid):
    """Handle CONCEDE PDU."""
    with lock:
        cur_state = game_state

    if cur_state != "IN_GAME":
        send_error(conn, "WRONG_PHASE",
            "CONCEDE only allowed during IN_GAME.", msg, msg.get("seq_num", 0))
        return

    if game_over:
        return

    pids = get_pids()
    if pid not in pids:
        return

    opp = get_opponent(pid)
    trigger_game_over("CONCEDE", opp, pid)

# ── Client handler ────────────────────────────────────────────────────────────

def handle_client(conn, addr):
    """Handle a single client connection."""
    global game_state

    print(f"[CONNECTED] {addr}")
    local_pid = None

    while True:
        try:
            msg = receive_pdu(conn)
            if msg is None:
                break

            msg_type = msg.get("type")
            msg_seq = msg.get("seq_num", 0)
            print(f"[RECV {addr}] {msg_type}")

            # ── PING ──────────────────────────────────────────────────
            if msg_type == "PING":
                send_pdu(conn, pong(msg["timestamp"], msg["seq_num"]))

            # ── PLAYER_READY ─────────────────────────────────────────
            elif msg_type == "PLAYER_READY":
                with lock:
                    cur = game_state

                if cur != "LOBBY":
                    send_error(conn, "WRONG_PHASE",
                        "PLAYER_READY is only accepted in LOBBY.", msg, msg_seq)
                    continue

                pid = msg.get("player_id", "").strip()
                deck_list = msg.get("deck_list", [])

                if not pid:
                    send_error(conn, "ILLEGAL_ACTION",
                        "player_id must be a non-empty string.", msg, msg_seq)
                    continue

                # Duplicate ID check
                others = [p for p, c in players.items() if c != conn]
                if pid in others:
                    send_error(conn, "DUPLICATE_ID",
                        f"'{pid}' is already taken by another player.", msg, msg_seq)
                    continue

                # Validate deck
                ok, deck_err = validate_deck(deck_list)
                if not ok:
                    send_error(conn, "ILLEGAL_DECK", deck_err, msg, msg_seq)
                    continue

                # Register (or re-register) player
                with lock:
                    players[pid] = conn
                    ready_data[pid] = {"deck": deck_list}
                    local_pid = pid

                print(f"[LOBBY] {pid} ready ({len(ready_data)}/2).")
                send_pdu(conn, lobby_update())

                if len(ready_data) == 2:
                    threading.Thread(target=run_game_setup, daemon=True).start()

            # ── MULLIGAN_CHOICE ──────────────────────────────────────
            elif msg_type == "MULLIGAN_CHOICE":
                with lock:
                    cur = game_state

                if cur != "MULLIGAN":
                    send_error(conn, "WRONG_PHASE",
                        "MULLIGAN_CHOICE is only accepted during MULLIGAN.", msg, msg_seq)
                    continue

                if local_pid is None:
                    send_error(conn, "ILLEGAL_ACTION",
                        "Send PLAYER_READY first.", msg, msg_seq)
                    continue

                handle_mulligan(conn, msg, local_pid)

            # ── CONCEDE ──────────────────────────────────────────────
            elif msg_type == "CONCEDE":
                if local_pid is None:
                    send_error(conn, "ILLEGAL_ACTION",
                        "Send PLAYER_READY first.", msg, msg_seq)
                    continue

                handle_concede(conn, msg, local_pid)

            # ── UNKNOWN ──────────────────────────────────────────────
            else:
                send_error(conn, "UNKNOWN_TYPE",
                    f"Unknown PDU type: '{msg_type}'", msg, msg_seq)

        except Exception as e:
            print(f"[ERROR] {addr}: {e}")
            break

    # ── Cleanup on disconnect ────────────────────────────────────────
    print(f"[DISCONNECTED] {addr}")
    conn.close()

    if conn in clients:
        clients.remove(conn)

    # If we were in a game and not already over, trigger GAME_OVER
    with lock:
        cur_state = game_state
        is_over = game_over

    if cur_state == "IN_GAME" and not is_over and local_pid:
        pids = get_pids()
        if local_pid in pids:
            remaining = [p for p in pids if p != local_pid]
            if remaining:
                trigger_game_over("DISCONNECT", remaining[0], local_pid)
                return

    # Otherwise, clean up the disconnected player from LOBBY/MULLIGAN
    for p, c in list(players.items()):
        if c == conn:
            del players[p]
            ready_data.pop(p, None)
            mulligan_kept.pop(p, None)
            last_state_seq.pop(p, None)
            break

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(2)
    print(f"[SERVER] Listening on {HOST}:{PORT}")

    while True:
        conn, addr = server.accept()

        if len(clients) >= 2:
            send_pdu(conn, {
                "type": "ERROR", "seq_num": 0,
                "code": "LOBBY_FULL",
                "message": "Lobby is full.",
                "rejected_action": {},
            })
            conn.close()
            continue

        clients.append(conn)
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()

if __name__ == "__main__":
    main()
