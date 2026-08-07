import socket
import threading
import random

from models.protocol import receive_pdu, send_pdu
from models.pdu import game_state_update, error, pong
from models.cards import all_legal_card_ids, get_card, is_creature
from server.priority_stack import PriorityStackEngine

HOST = "127.0.0.1"
PORT = 4444

# Global state
clients = []                # connected sockets (max 2)
players = {}                # player_id -> socket
ready_data = {}             # player_id -> {"deck": [...]}
game_data = {}              # player_id -> {"life", "hand", "library", "mulligan_count", "battlefield", "graveyard"}
mulligan_kept = {}          # player_id -> True once they keep
last_state_seq = {}         # player_id -> seq_num of the last GAME_STATE_UPDATE sent to them

game_state = "LOBBY"        # LOBBY | GAME_SETUP | MULLIGAN | IN_GAME | GAME_OVER
server_seq = 0
lock = threading.Lock()
mull_lock = threading.Lock()

# IN_GAME specific state
turn_number = 0
active_player = None        # player_id of the current active player
current_phase = None
game_over = False
engine = None               # PriorityStackEngine

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

LEGAL_CARDS = all_legal_card_ids()

def validate_deck(deck):
    if not deck:
        return False, "Deck is empty; minimum is 1 card."
    if len(deck) > 50:
        return False, f"Deck has {len(deck)} cards; maximum is 50."
    bad = [c for c in deck if c not in LEGAL_CARDS]
    if bad:
        return False, f"Unknown card(s): {bad}"
    return True, None

def lobby_update():
    waiting = [p for p in players if p not in ready_data]
    return game_state_update({
        "phase":         "LOBBY",
        "players_ready": len(ready_data),
        "waiting_for":   waiting,
    }, next_seq())

def _format_battlefield_for_state(pid: str) -> list[dict]:
    formatted = []
    for perm in game_data[pid].get("battlefield", []):
        card = get_card(perm["card_id"])
        item = {
            "id": perm.get("id", perm.get("instance_id")),
            "instance_id": perm.get("instance_id", perm.get("id")),
            "card_id": perm["card_id"],
            "tapped": perm.get("tapped", False),
            "damage": perm.get("damage", 0),
            "summoning_sick": perm.get("summoning_sick", True),
        }
        if is_creature(perm["card_id"]):
            base_p = card.get("power") or 0
            base_t = card.get("toughness") or 0
            item.update({
                "power": base_p + perm.get("pump_power", 0),
                "toughness": base_t + perm.get("pump_toughness", 0),
            })
        formatted.append(item)
    return formatted

def broadcast_game_state():
    """Broadcast a personalized GAME_STATE_UPDATE to both players (RFC 0001 Section 10.2.2)."""
    pids = get_pids()
    if not pids:
        return
    seq = next_seq()
    for pid in pids:
        opp = get_opponent(pid)
        state = {
            "turn": turn_number,
            "active_player": active_player,
            "phase": current_phase or "UNTAP",
            "priority_player": engine.priority_player if (engine and current_phase not in ("UNTAP", "CLEANUP")) else None,
            "priority_holder": engine.priority_player if (engine and current_phase not in ("UNTAP", "CLEANUP")) else None,
            "life_totals": {p: game_data[p]["life"] for p in pids},
            "stack": engine.stack if engine else [],
            "battlefield": {p: _format_battlefield_for_state(p) for p in pids},
            "graveyard": {p: game_data[p].get("graveyard", []) for p in pids},
            "hand": {pid: game_data[pid]["hand"]},
            "hand_counts": {opp: len(game_data[opp]["hand"])},
            "library_counts": {p: len(game_data[p]["library"]) for p in pids},
            "land_played_this_turn": pid in engine.land_played_this_turn if engine else False,
        }
        send_to(pid, game_state_update(state, seq))

def trigger_game_over(reason, winner_id, loser_id):
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
    global game_state, game_data, mulligan_kept, last_state_seq
    global turn_number, current_phase, active_player, game_over, engine

    with lock:
        game_state = "LOBBY"
        game_data.clear()
        mulligan_kept.clear()
        last_state_seq.clear()
        turn_number = 0
        current_phase = None
        active_player = None
        game_over = False
        engine = None
        players.clear()
        ready_data.clear()

    print("[RESET] Returned to LOBBY state. Send new PLAYER_READY.")

def check_win_conditions():
    pids = get_pids()
    if not pids:
        return False
    for pid in pids:
        if game_data[pid]["life"] <= 0:
            opp = get_opponent(pid)
            trigger_game_over("LIFE_ZERO", opp, pid)
            return True
        if len(game_data[pid]["library"]) == 0:
            opp = get_opponent(pid)
            trigger_game_over("DECK_EMPTY", opp, pid)
            return True
    return False

class _EngineCtx:
    game_data = None
    @staticmethod
    def get_pids(): return get_pids()
    @staticmethod
    def get_opponent(pid): return get_opponent(pid)
    @staticmethod
    def next_seq(): return next_seq()
    @staticmethod
    def send_to(pid, msg): send_to(pid, msg)
    @staticmethod
    def broadcast(msg): broadcast(msg)
    @staticmethod
    def broadcast_game_state(): broadcast_game_state()
    @staticmethod
    def check_win_conditions(): return check_win_conditions()
    @staticmethod
    def trigger_game_over(reason, winner, loser): trigger_game_over(reason, winner, loser)
    @staticmethod
    def send_error(conn, code, text, rejected, seq): send_error(conn, code, text, rejected, seq)
    lock = lock

    @property
    def active_player(self): return active_player
    @active_player.setter
    def active_player(self, value):
        global active_player
        active_player = value

    @property
    def current_phase(self): return current_phase
    @current_phase.setter
    def current_phase(self, value):
        global current_phase
        current_phase = value

    @property
    def turn_number(self): return turn_number
    @turn_number.setter
    def turn_number(self, value):
        global turn_number
        turn_number = value

    @property
    def game_over(self): return game_over

def start_in_game():
    global game_state, turn_number, current_phase, engine, game_over

    ctx = _EngineCtx()
    ctx.game_data = game_data
    engine = PriorityStackEngine(ctx)

    with lock:
        game_state = "IN_GAME"
        turn_number = 1
        current_phase = "UNTAP"
        game_over = False

    broadcast({
        "type": "PHASE_TRANSITION",
        "seq_num": next_seq(),
        "from_phase": "MULLIGAN",
        "to_phase": "UNTAP",
        "active_player": active_player,
        "turn": turn_number,
    })

    print(f"[IN_GAME] Started. Active player: {active_player}, Turn {turn_number}")
    engine.begin_turn()

def run_game_setup():
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
            "mana_pool": {},
        }

    with lock:
        game_state = "MULLIGAN"

    seq = next_seq()
    for pid in pids:
        opp = get_opponent(pid)
        state = {
            "turn": 0,
            "phase": "MULLIGAN",
            "active_player": active_player,
            "life_totals": {p: game_data[p]["life"] for p in pids},
            "hand": {pid: game_data[pid]["hand"]},
            "hand_counts": {opp: len(game_data[opp]["hand"])},
            "library_counts": {p: len(game_data[p]["library"]) for p in pids},
            "battlefield": {p: [] for p in pids},
            "graveyard": {p: [] for p in pids},
            "stack": [],
        }
        last_state_seq[pid] = seq
        send_to(pid, game_state_update(state, seq))

def handle_mulligan(conn, msg, pid):
    keep = msg.get("keep", True)
    to_bot = msg.get("cards_to_bottom", [])
    msg_seq = msg.get("seq_num", 0)
    pdata = game_data[pid]
    pids = get_pids()

    expected_seq = last_state_seq.get(pid, 0)
    if msg_seq != expected_seq:
        send_error(conn, "STALE_ACTION", f"Priority token mismatch. Expected seq_num {expected_seq}, got {msg_seq}.", msg, msg_seq)
        return

    if mulligan_kept.get(pid, False):
        send_error(conn, "ILLEGAL_ACTION", f"Player {pid} has already kept their hand.", msg, msg_seq)
        return

    if keep:
        if len(to_bot) != pdata["mulligan_count"]:
            send_error(conn, "ILLEGAL_ACTION", f"cards_to_bottom must have exactly {pdata['mulligan_count']} card(s), got {len(to_bot)}.", msg, msg_seq)
            return

        for c in to_bot:
            if c not in pdata["hand"]:
                send_error(conn, "ILLEGAL_ACTION", f"Card '{c}' is not in your hand.", msg, msg_seq)
                return

        for c in to_bot:
            pdata["hand"].remove(c)
            pdata["library"].append(c)

        with mull_lock:
            mulligan_kept[pid] = True
            both_kept = all(mulligan_kept.get(p) for p in pids)

        if both_kept:
            start_in_game()
    else:
        pdata["mulligan_count"] += 1
        pdata["library"] = pdata["hand"] + pdata["library"]
        random.shuffle(pdata["library"])
        pdata["hand"] = pdata["library"][:7]
        pdata["library"] = pdata["library"][7:]

        new_seq = next_seq()
        opp = get_opponent(pid)
        state = {
            "turn": 0,
            "phase": "MULLIGAN",
            "active_player": active_player,
            "life_totals": {p: game_data[p]["life"] for p in pids},
            "hand": {pid: pdata["hand"]},
            "hand_counts": {opp: len(game_data[opp]["hand"])},
            "library_counts": {p: len(game_data[p]["library"]) for p in pids},
            "battlefield": {p: [] for p in pids},
            "graveyard": {p: [] for p in pids},
            "stack": [],
        }
        last_state_seq[pid] = new_seq
        send_to(pid, game_state_update(state, new_seq))

def handle_concede(conn, msg, pid):
    if game_state != "IN_GAME":
        send_error(conn, "WRONG_PHASE", "CONCEDE only allowed during IN_GAME.", msg, msg.get("seq_num", 0))
        return
    if game_over:
        return
    opp = get_opponent(pid)
    trigger_game_over("CONCEDE", opp, pid)

def handle_client(conn, addr):
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

            if msg_type == "PING":
                send_pdu(conn, pong(msg["timestamp"], msg["seq_num"]))

            elif msg_type == "PLAYER_READY":
                if game_state != "LOBBY":
                    send_error(conn, "WRONG_PHASE", "PLAYER_READY is only accepted in LOBBY.", msg, msg_seq)
                    continue

                pid = msg.get("player_id", "").strip()
                deck_list = msg.get("deck_list", [])

                if not pid:
                    send_error(conn, "ILLEGAL_ACTION", "player_id must be a non-empty string.", msg, msg_seq)
                    continue

                others = [p for p, c in players.items() if c != conn]
                if pid in others:
                    send_error(conn, "DUPLICATE_ID", f"'{pid}' is already taken by another player.", msg, msg_seq)
                    continue

                ok, deck_err = validate_deck(deck_list)
                if not ok:
                    send_error(conn, "ILLEGAL_DECK", deck_err, msg, msg_seq)
                    continue

                with lock:
                    players[pid] = conn
                    ready_data[pid] = {"deck": deck_list}
                    local_pid = pid

                send_pdu(conn, lobby_update())

                if len(ready_data) == 2:
                    threading.Thread(target=run_game_setup, daemon=True).start()

            elif msg_type == "MULLIGAN_CHOICE":
                if game_state != "MULLIGAN":
                    send_error(conn, "WRONG_PHASE", "MULLIGAN_CHOICE is only accepted during MULLIGAN.", msg, msg_seq)
                    continue
                handle_mulligan(conn, msg, local_pid)

            elif msg_type == "CONCEDE":
                handle_concede(conn, msg, local_pid)

            elif msg_type == "PRIORITY_PASS":
                if game_state != "IN_GAME" or engine is None:
                    send_error(conn, "WRONG_PHASE", "PRIORITY_PASS only accepted during IN_GAME.", msg, msg_seq)
                    continue
                engine.handle_priority_pass(conn, msg, local_pid)

            elif msg_type == "CAST_SPELL":
                if game_state != "IN_GAME" or engine is None:
                    send_error(conn, "WRONG_PHASE", "CAST_SPELL only accepted during IN_GAME.", msg, msg_seq)
                    continue
                engine.handle_cast_spell(conn, msg, local_pid)

            elif msg_type == "ACTIVATE_ABILITY":
                if game_state != "IN_GAME" or engine is None:
                    send_error(conn, "WRONG_PHASE", "ACTIVATE_ABILITY only accepted during IN_GAME.", msg, msg_seq)
                    continue
                engine.handle_activate_ability(conn, msg, local_pid)

            elif msg_type == "PLAY_LAND":
                if game_state != "IN_GAME" or engine is None:
                    send_error(conn, "WRONG_PHASE", "PLAY_LAND only accepted during IN_GAME.", msg, msg_seq)
                    continue
                engine.handle_play_land(conn, msg, local_pid)

            elif msg_type == "DECLARE_ATTACKERS":
                if game_state != "IN_GAME" or engine is None:
                    send_error(conn, "WRONG_PHASE", "DECLARE_ATTACKERS only accepted during IN_GAME.", msg, msg_seq)
                    continue
                engine.handle_declare_attackers(conn, msg, local_pid)

            elif msg_type == "DECLARE_BLOCKERS":
                if game_state != "IN_GAME" or engine is None:
                    send_error(conn, "WRONG_PHASE", "DECLARE_BLOCKERS only accepted during IN_GAME.", msg, msg_seq)
                    continue
                engine.handle_declare_blockers(conn, msg, local_pid)

            elif msg_type == "ASSIGN_DAMAGE_ORDER":
                if game_state != "IN_GAME" or engine is None:
                    send_error(conn, "WRONG_PHASE", "ASSIGN_DAMAGE_ORDER only accepted during IN_GAME.", msg, msg_seq)
                    continue
                engine.handle_assign_damage_order(conn, msg, local_pid)

            elif msg_type == "DISCARD":
                if game_state != "IN_GAME" or engine is None:
                    send_error(conn, "WRONG_PHASE", "DISCARD only accepted during IN_GAME.", msg, msg_seq)
                    continue
                engine.handle_discard(conn, msg, local_pid)

            elif msg_type == "TRIGGER_CHOICE_RESPONSE":
                if game_state != "IN_GAME" or engine is None:
                    send_error(conn, "WRONG_PHASE", "TRIGGER_CHOICE_RESPONSE only accepted during IN_GAME.", msg, msg_seq)
                    continue
                engine.handle_trigger_choice_response(conn, msg, local_pid)

            elif msg_type == "TRIGGER_ORDER_RESPONSE":
                if game_state != "IN_GAME" or engine is None:
                    send_error(conn, "WRONG_PHASE", "TRIGGER_ORDER_RESPONSE only accepted during IN_GAME.", msg, msg_seq)
                    continue
                engine.handle_trigger_order_response(conn, msg, local_pid)

            else:
                send_error(conn, "UNKNOWN_TYPE", f"Unknown PDU type: '{msg_type}'", msg, msg_seq)

        except Exception as e:
            print(f"[ERROR] {addr}: {e}")
            break

    print(f"[DISCONNECTED] {addr}")
    conn.close()
    if conn in clients:
        clients.remove(conn)

    if game_state == "IN_GAME" and not game_over and local_pid:
        pids = get_pids()
        if local_pid in pids:
            remaining = [p for p in pids if p != local_pid]
            if remaining:
                trigger_game_over("DISCONNECT", remaining[0], local_pid)

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