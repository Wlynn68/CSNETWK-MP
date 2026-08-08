import random

from models.pdu import game_state_update, error as error_pdu, game_over as game_over_pdu
from models.protocol import send_pdu
from models.cards import get_card, is_creature


class GameState:

    def __init__(self, player_ids, decks, seq_provider, broadcast_fn, send_fn, on_game_over=None):
        self._player_ids = list(player_ids)
        self._next_seq = seq_provider      # server's next_sequence()
        self._broadcast_raw = broadcast_fn  # server.broadcast(message)
        self._send_raw = send_fn            # server.send_to_player(player_id, message)
        self._on_game_over = on_game_over   # server.reset_to_lobby()

        self.game_data = {}
        for pid in self._player_ids:
            self.game_data[pid] = {
                "hand": [],
                "battlefield": [],
                "graveyard": [],
                "library": decks[pid][:],
                "life": 20,
                "mana_pool": {},
            }

        # Mulligan bookkeeping (London mulligan; RFC 0001 s6.4)
        self.mulligan_count = {pid: 0 for pid in self._player_ids}
        self.kept = {pid: False for pid in self._player_ids}

        self.active_player = None
        self.turn_number = 1
        self.current_phase = "MULLIGAN"
        self.game_over = False

        self.engine = None  # PriorityStackEngine, attached by server.py after construction

        # Tracks the seq_num of the most recent PDU sent to each player,
        # used to validate MULLIGAN_CHOICE / DISCARD / CONCEDE echoes (RFC 0001 s5.4).
        self.last_seq_sent = {pid: 0 for pid in self._player_ids}

    # ---- setup helpers (used once, during GAME_SETUP) ----

    def shuffle_all(self):
        for pdata in self.game_data.values():
            random.shuffle(pdata["library"])

    def draw_n(self, pid, n):
        pdata = self.game_data[pid]
        for _ in range(n):
            if not pdata["library"]:
                return False
            pdata["hand"].append(pdata["library"].pop(0))
        return True

    # ---- ctx interface required by PriorityStackEngine ----

    def get_pids(self):
        return list(self._player_ids)

    def get_opponent(self, pid):
        others = [p for p in self._player_ids if p != pid]
        return others[0] if others else None

    def next_seq(self):
        return self._next_seq()

    def send_to(self, pid, message):
        self.last_seq_sent[pid] = message.get("seq_num", self.last_seq_sent.get(pid, 0))
        self._send_raw(pid, message)

    def broadcast(self, message):
        for pid in self._player_ids:
            self.last_seq_sent[pid] = message.get("seq_num", self.last_seq_sent.get(pid, 0))
        self._broadcast_raw(message)

    def send_error(self, conn, code, message, rejected_action, seq_num):
        send_pdu(conn, error_pdu(code, message, rejected_action, seq_num))

    def send_state_to(self, pid):
        """Send a personalized GAME_STATE_UPDATE to a single player. Used during
        MULLIGAN (redraw / partial keep) where only one player's state changed."""
        state = self.visible_state(pid)
        msg = game_state_update(state, self.next_seq())
        self.send_to(pid, msg)

    def broadcast_game_state(self):
        """Send a personalized GAME_STATE_UPDATE to each player (hidden hands filtered)."""
        for pid in self._player_ids:
            state = self.visible_state(pid)
            msg = game_state_update(state, self.next_seq())
            self.send_to(pid, msg)

    def visible_state(self, viewer_id):
        priority_holder = self.engine.priority_player if self.engine else None

        stack_view = []
        if self.engine:
            for item in self.engine.stack:
                stack_view.append({
                    "stack_item_id": item["stack_item_id"],
                    "item_type": item["item_type"],
                    "source": item["source"],
                    "targets": item.get("targets", []),
                    "controller": item["controller"],
                })

        land_played = bool(
            self.engine and self.active_player in self.engine.land_played_this_turn
        )

        state = {
            "turn": self.turn_number,
            "active_player": self.active_player,
            "phase": self.current_phase,
            "priority_holder": priority_holder,
            "life_totals": {},
            "battlefield": {},
            "graveyard": {},
            "hand": {},
            "hand_counts": {},
            "library_counts": {},
            "stack": stack_view,
            "land_played_this_turn": land_played,
        }

        for pid in self._player_ids:
            pdata = self.game_data[pid]
            state["life_totals"][pid] = pdata["life"]
            state["graveyard"][pid] = pdata["graveyard"]
            state["library_counts"][pid] = len(pdata["library"])
            state["hand_counts"][pid] = len(pdata["hand"])
            state["battlefield"][pid] = [self._render_permanent(p) for p in pdata["battlefield"]]

        # Only the viewer's own hand is revealed.
        state["hand"][viewer_id] = self.game_data[viewer_id]["hand"]

        return state

    @staticmethod
    def _render_permanent(perm):
        card = get_card(perm["card_id"]) or {}
        rendered = {"id": perm.get("id", perm.get("instance_id")), "tapped": perm.get("tapped", False)}
        if is_creature(perm["card_id"]):
            power = (card.get("power") or 0) + perm.get("pump_power", 0)
            toughness = (card.get("toughness") or 0) + perm.get("pump_toughness", 0)
            rendered.update({
                "damage": perm.get("damage", 0),
                "power": power,
                "toughness": toughness,
                "summoning_sick": perm.get("summoning_sick", False),
            })
        return rendered

    def check_win_conditions(self):
        """Called after cleanup; returns True if the game has already ended."""
        if self.game_over:
            return True
        for pid in self._player_ids:
            if self.game_data[pid]["life"] <= 0:
                self.trigger_game_over("LIFE_ZERO", self.get_opponent(pid), pid)
                return True
        return False

    def trigger_game_over(self, reason, winner_id, loser_id):
        if self.game_over:
            return
        self.game_over = True
        self.broadcast(game_over_pdu(winner_id, loser_id, reason, self.next_seq()))
        print(f"[GAME] GAME_OVER reason={reason} winner={winner_id} loser={loser_id}")
        if self._on_game_over:
            self._on_game_over()