import socket
import threading
import random
import sys

from models.protocol import send_pdu, receive_pdu, set_verbose, InvalidPDUError, PDUTooLargeError
from models.pdu import error, pong
from models.cards import all_legal_card_ids
from models.game_state import GameState
from server.priority_stack import PriorityStackEngine

HOST = "127.0.0.1"
PORT = 4444

MAX_PLAYERS = 2

clients = [] 
players = {}

game_started = False
gs: GameState | None = None

game_seq = 1
state_lock = threading.Lock()

# Guards all reads/writes of `players`, `gs`, and `game_started`. Two client
# threads can otherwise race on PLAYER_READY / game actions arriving at the
# same instant (e.g. "dictionary changed size during iteration").
game_lock = threading.RLock()

def next_sequence():
    global game_seq
    with state_lock:
        value = game_seq
        game_seq += 1
        return value


def send_to_player(player_id, message):
    if player_id not in players:
        return
    conn = players[player_id]["conn"]
    try:
        send_pdu(conn, message)
    except Exception as e:
        print(f"[SEND ERROR] {player_id}: {e}")


def broadcast(message):
    for conn in clients[:]:
        try:
            send_pdu(conn, message)
        except Exception as e:
            print(f"[BROADCAST ERROR] {e}")


def pid_for_conn(conn):
    for pid, pdata in players.items():
        if pdata["conn"] == conn:
            return pid
    return None


def validate_deck(deck):
    if not isinstance(deck, list):
        return False
    if len(deck) < 1 or len(deck) > 50:
        return False
    try:
        legal_cards = all_legal_card_ids()
    except Exception as e:
        print(f"[WARNING] Could not load legal card IDs: {e}")
        return False

    for card_id in deck:
        if card_id not in legal_cards:
            print(f"[DECK ERROR] Illegal card: {card_id}")
            return False

    return True


# ---------------------------------------------------------------------------
# LOBBY
# ---------------------------------------------------------------------------

def send_lobby_state():
    """RFC 0001 s6.2: respond to every PLAYER_READY with a lobby-phase GAME_STATE_UPDATE."""
    ready_ids = list(players.keys())
    open_slots = max(0, MAX_PLAYERS - len(players))

    for pid, pdata in players.items():
        state = {
            "phase": "LOBBY",
            "players_ready": len(players),
            "waiting_for": [] if open_slots == 0 else ["(waiting for opponent)"] * open_slots,
        }
        send_pdu(pdata["conn"], {
            "type": "GAME_STATE_UPDATE",
            "seq_num": next_sequence(),
            "state": state,
        })


def create_player_state(player_id, conn, deck):
    return {
        "player_id": player_id,
        "conn": conn,
        "deck": deck[:],
    }


def handle_player_ready(conn, message):
    global game_started

    player_id = message.get("player_id")
    deck = message.get("deck_list", [])
    seq_num = message.get("seq_num", 0)

    if game_started:
        send_pdu(conn, error("GAME_IN_PROGRESS", "A game is already in progress.", "PLAYER_READY", seq_num))
        return

    if not player_id:
        send_pdu(conn, error("INVALID_ID", "Player ID cannot be empty.", "PLAYER_READY", seq_num))
        return

    if player_id in players and players[player_id]["conn"] is not conn:
        # A *different* connection is trying to claim an ID already in use.
        send_pdu(conn, error("DUPLICATE_ID", "Player ID is already taken.", "PLAYER_READY", seq_num))
        return

    if not validate_deck(deck):
        send_pdu(conn, error("ILLEGAL_DECK", "The submitted deck is invalid.", "PLAYER_READY", seq_num))
        return

    if player_id not in players and len(players) >= MAX_PLAYERS:
        send_pdu(conn, error("LOBBY_FULL", "The lobby is already full.", "PLAYER_READY", seq_num))
        return

    # New submission, or the same connection replacing its earlier deck list.
    players[player_id] = create_player_state(player_id, conn, deck)

    print(f"[LOBBY] {player_id} is ready.")
    print(f"[LOBBY] Players ready: {len(players)}/{MAX_PLAYERS}")

    send_lobby_state()

    if len(players) == MAX_PLAYERS:
        start_game()


# ---------------------------------------------------------------------------
# GAME_SETUP -> MULLIGAN
# ---------------------------------------------------------------------------

def start_game():
    global game_started, gs

    if len(players) != MAX_PLAYERS:
        return

    print()
    print("=" * 60)
    print("[GAME] Both players are ready.")
    print("[GAME] Starting game setup...")
    print("=" * 60)

    pids = list(players.keys())
    decks = {pid: players[pid]["deck"] for pid in pids}

    new_gs = GameState(
        player_ids=pids,
        decks=decks,
        seq_provider=next_sequence,
        broadcast_fn=broadcast,
        send_fn=send_to_player,
        on_game_over=reset_to_lobby,
    )

    new_gs.shuffle_all()
    for pid in pids:
        print(f"[GAME] Shuffled {pid}'s deck.")

    for pid in pids:
        if not new_gs.draw_n(pid, 7):
            print(f"[ERROR] Could not draw opening hand for {pid}.")
            return
        print(f"[GAME] {pid} drew {len(new_gs.game_data[pid]['hand'])} cards.")

    new_gs.active_player = random.choice(pids)
    new_gs.engine = PriorityStackEngine(new_gs)

    gs = new_gs
    game_started = True

    print()
    print(f"[GAME] First player: {gs.active_player}")
    print("[GAME] Phase: MULLIGAN")
    print("=" * 60)
    print()

    gs.broadcast_game_state()


def handle_mulligan_choice(conn, message):
    seq_num = message.get("seq_num", 0)

    if gs is None or gs.current_phase != "MULLIGAN":
        send_pdu(conn, error("ILLEGAL_ACTION", "No mulligan decision is currently open.", "MULLIGAN_CHOICE", seq_num))
        return

    pid = pid_for_conn(conn)
    if pid is None:
        send_pdu(conn, error("ILLEGAL_ACTION", "Connection is not part of the current game.", "MULLIGAN_CHOICE", seq_num))
        return

    if gs.kept.get(pid):
        send_pdu(conn, error("ILLEGAL_ACTION", "You have already kept your hand.", message, seq_num))
        return

    expected_seq = gs.last_seq_sent.get(pid)
    if seq_num != expected_seq:
        send_pdu(conn, error(
            "STALE_ACTION",
            f"Priority token mismatch. Expected seq_num {expected_seq}, got {seq_num}.",
            message, seq_num,
        ))
        return

    keep = message.get("keep", False)
    cards_to_bottom = message.get("cards_to_bottom", [])

    if not keep:
        gs.mulligan_count[pid] += 1
        pdata = gs.game_data[pid]
        pdata["library"].extend(pdata["hand"])
        pdata["hand"] = []
        random.shuffle(pdata["library"])
        gs.draw_n(pid, 7)
        print(f"[MULLIGAN] {pid} mulliganed (count={gs.mulligan_count[pid]}).")
        gs.send_state_to(pid)
        return

    # keep == True: must bottom exactly `mulligan_count` cards.
    needed = gs.mulligan_count[pid]
    if len(cards_to_bottom) != needed:
        send_pdu(conn, error(
            "ILLEGAL_ACTION",
            f"Must bottom exactly {needed} card(s) to keep, got {len(cards_to_bottom)}.",
            message, seq_num,
        ))
        return

    pdata = gs.game_data[pid]
    for cid in cards_to_bottom:
        if cid not in pdata["hand"]:
            send_pdu(conn, error("ILLEGAL_ACTION", f"'{cid}' is not in your hand.", message, seq_num))
            return

    for cid in cards_to_bottom:
        pdata["hand"].remove(cid)
        pdata["library"].append(cid)

    gs.kept[pid] = True
    print(f"[MULLIGAN] {pid} kept their hand ({needed} card(s) bottomed).")

    if all(gs.kept.values()):
        begin_in_game()
    else:
        gs.send_state_to(pid)


def begin_in_game():
    gs.turn_number = 1
    gs.current_phase = "UNTAP"
    print()
    print("=" * 60)
    print(f"[GAME] Both players kept. Beginning turn 1. Active player: {gs.active_player}")
    print("=" * 60)
    print()
    gs.engine.begin_turn()


# ---------------------------------------------------------------------------
# IN_GAME dispatch -> PriorityStackEngine
# ---------------------------------------------------------------------------

GAME_ACTION_HANDLERS = {
    "PRIORITY_PASS": "handle_priority_pass",
    "CAST_SPELL": "handle_cast_spell",
    "ACTIVATE_ABILITY": "handle_activate_ability",
    "PLAY_LAND": "handle_play_land",
    "DECLARE_ATTACKERS": "handle_declare_attackers",
    "DECLARE_BLOCKERS": "handle_declare_blockers",
    "ASSIGN_DAMAGE_ORDER": "handle_assign_damage_order",
    "DISCARD": "handle_discard",
    "TRIGGER_ORDER_RESPONSE": "handle_trigger_order_response",
    "TRIGGER_CHOICE_RESPONSE": "handle_trigger_choice_response",
}


def handle_game_action(conn, message, msg_type):
    seq_num = message.get("seq_num", 0)

    if gs is None or not game_started or gs.current_phase == "MULLIGAN":
        send_pdu(conn, error("WRONG_PHASE", "No active game in progress for this action.", message, seq_num))
        return

    pid = pid_for_conn(conn)
    if pid is None:
        send_pdu(conn, error("ILLEGAL_ACTION", "Connection is not part of the current game.", message, seq_num))
        return

    handler = getattr(gs.engine, GAME_ACTION_HANDLERS[msg_type])
    handler(conn, message, pid)


def handle_concede(conn, message):
    seq_num = message.get("seq_num", 0)

    if gs is None:
        send_pdu(conn, error("ILLEGAL_ACTION", "No game in progress.", message, seq_num))
        return

    pid = pid_for_conn(conn)
    if pid is None or pid != message.get("player_id"):
        send_pdu(conn, error("ILLEGAL_ACTION", "Invalid CONCEDE.", message, seq_num))
        return

    # CONCEDE is exempt from the priority-echo rule: seq_num must match the
    # most recently received server PDU of any type (RFC 0001 s5.4).
    expected_seq = gs.last_seq_sent.get(pid)
    if seq_num != expected_seq:
        send_pdu(conn, error(
            "STALE_ACTION",
            f"seq_num does not match the most recent PDU sent to you. Expected {expected_seq}, got {seq_num}.",
            message, seq_num,
        ))
        return

    gs.trigger_game_over("CONCEDE", gs.get_opponent(pid), pid)


def reset_to_lobby():
    """Called once GAME_OVER has been broadcast. Returns the server to LOBBY
    on the same TCP connections, per RFC 0001 s6.6."""
    global players, gs, game_started

    players = {}
    gs = None
    game_started = False
    print("[GAME] Returning to LOBBY. Awaiting new PLAYER_READY PDUs.")


# ---------------------------------------------------------------------------
# Misc PDU handlers
# ---------------------------------------------------------------------------

def handle_ping(conn, message):
    timestamp = message.get("timestamp")
    send_pdu(conn, pong(timestamp, next_sequence()))


def handle_unknown_message(conn, message):
    msg_type = message.get("type", "UNKNOWN")
    seq_num = message.get("seq_num", 0)
    send_pdu(conn, error("UNKNOWN_TYPE", f"Unsupported PDU type: {msg_type}", msg_type, seq_num))


def handle_invalid_json(conn, detail: str):
    """RFC 0001 §11: bytes that don't parse as valid UTF-8 JSON get ERROR
    code INVALID_JSON. The action is discarded and the game state is left
    unchanged; if the sender still holds priority, PRIORITY_GRANT is
    re-issued so they can retry (§11 point 3). We have no seq_num to echo
    since the payload never parsed, so we send 0 ("not available")."""
    print(f"[INVALID_JSON] {detail}")
    send_pdu(conn, error("INVALID_JSON", detail, None, 0))

    pid = pid_for_conn(conn)
    if (
        pid
        and gs is not None
        and game_started
        and not gs.game_over
        and gs.engine is not None
        and gs.engine.priority_player == pid
    ):
        gs.engine.grant_priority(pid)


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------

def handle_client(conn, addr):
    print(f"[CONNECTED] {addr}")

    try:
        while True:
            try:
                message = receive_pdu(conn)
            except PDUTooLargeError as e:
                # Oversized frame: the body was deliberately not read off the
                # socket, so the stream can no longer be trusted to be in
                # sync. Best-effort notify, then treat like a disconnect.
                print(f"[PDU_TOO_LARGE] {addr}: {e}")
                try:
                    send_pdu(conn, error("INVALID_JSON", str(e), None, 0))
                except Exception:
                    pass
                break
            except InvalidPDUError as e:
                # Malformed-but-well-framed PDU: recoverable per RFC 0001
                # §11 — report it and keep the connection (and game) going.
                with game_lock:
                    handle_invalid_json(conn, str(e))
                continue

            if message is None:
                break

            msg_type = message.get("type")

            if msg_type == "PING":
                handle_ping(conn, message)
            else:
                print(f"[RECEIVED] {msg_type}")
                # Everything else mutates shared state (players / gs) and
                # must be processed atomically with respect to the other
                # player's thread.
                with game_lock:
                    if msg_type == "PLAYER_READY":
                        handle_player_ready(conn, message)
                    elif msg_type == "MULLIGAN_CHOICE":
                        handle_mulligan_choice(conn, message)
                    elif msg_type == "CONCEDE":
                        handle_concede(conn, message)
                    elif msg_type in GAME_ACTION_HANDLERS:
                        handle_game_action(conn, message, msg_type)
                    else:
                        handle_unknown_message(conn, message)

    except ConnectionResetError:
        print(f"[DISCONNECTED] {addr}")

    except Exception as e:
        print(f"[ERROR] Client {addr}: {e}")

    finally:
        with game_lock:
            disconnected_player = pid_for_conn(conn)

            if conn in clients:
                clients.remove(conn)

            # A mid-game disconnect ends the game immediately: the remaining
            # player wins by DISCONNECT and the server returns to LOBBY,
            # retaining the surviving player's TCP connection (RFC 0001 s4.2, s6.6).
            if disconnected_player and gs is not None and game_started and not gs.game_over:
                survivor = gs.get_opponent(disconnected_player)
                print(f"[GAME] {disconnected_player} disconnected mid-game.")
                gs.trigger_game_over("DISCONNECT", survivor, disconnected_player)
            elif disconnected_player and disconnected_player in players:
                del players[disconnected_player]
                print(f"[LOBBY] Removed player {disconnected_player}")

        try:
            conn.close()
        except Exception:
            pass


def main():
    if "--verbose" in sys.argv:
        set_verbose(True)
        print("[SERVER] Verbose mode enabled.")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(MAX_PLAYERS)

    print()
    print("=" * 60)
    print("        MTGNP GAME SERVER")
    print("=" * 60)
    print(f"Listening on {HOST}:{PORT}")
    print(f"Maximum players: {MAX_PLAYERS}")
    print("=" * 60)
    print()

    while True:
        try:
            conn, addr = server.accept()

            if len(clients) >= MAX_PLAYERS:
                send_pdu(conn, error("LOBBY_FULL", "The lobby is full.", "", 0))
                conn.close()
                continue

            clients.append(conn)

            thread = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            thread.start()

        except KeyboardInterrupt:
            print()
            print("[SERVER] Shutting down...")
            break

        except Exception as e:
            print(f"[SERVER ERROR] {e}")

    server.close()


if __name__ == "__main__":
    main()
