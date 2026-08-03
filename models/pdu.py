def player_ready(player_id, deck, seq_num):
    return {
        "type": "PLAYER_READY",
        "seq_num": seq_num,              # monotonically increasing message counter
        "player_id": player_id,          # client-chosen non-empty string; must be unique in this lobby
        "deck_list": deck
    }

def game_state_update(state, seq_num):
    return {
        "type": "GAME_STATE_UPDATE",
        "seq_num": seq_num,
        "state": state
    }

def mulligan_choice(keep, cards_bottom, seq_num):
    return{
        "type": "MULLIGAN_CHOICE",
        "seq_num": seq_num, 
        "keep": keep,             # false = take a mulligan
        "cards_to_bottom": cards_bottom    # must equal mulligan count when keep=true
    }

def phase_transition(from_phase, to_phase, active_player, seq_num, turn):
    return{
        "type": "PHASE_TRANSITION",
        "seq_num": seq_num,              # server-issued sequence number
        "from_phase": from_phase,
        "to_phase": to_phase,
        "active_player": active_player,
        "turn": turn
    }
   
def priority_grant(player_id, seq_num, time_limit):
    return{
        "type": "PRIORITY_GRANT",
        "player_id": player_id,
        "seq_num": seq_num,            # server-issued sequence number
        "time_limit_ms": time_limit           # server-enforced response deadline
    }

def priotity_pass(seq_num):
    return{
        "type": "PRIORITY_PASS",
        "seq_num": seq_num            # must match current PRIORITY_GRANT seq_num
    }

def cast_spell(card_id, targets, mana_payment, seq_num):
    return{
        "type": "CAST_SPELL",
        "seq_num": seq_num,
        "card_id": card_id,
        "targets": targets,    # empty array if spell has no targets
        "mana_payment": mana_payment       # color keys: W U B R G, generic key: "X"
    }

def activate_ability(source, ability_index, targets, seq_num, cost_payment):
    return{
        "type": "ACTIVATE_ABILITY",
        "seq_num": seq_num,
        "source_id": source,
        "ability_index": ability_index, # 0-based index into permanent's ability list
        "targets": targets,
        "cost_payment":  cost_payment # { "tap": true, "mana": {} }, tap: true only if ability requires tapping
        # Server rejects with ILLEGAL_ACTION if permanent is already tapped
    }

def stack_push(item_id, item_type, source, targets, seq_num, controller):
    return{
        "type": "STACK_PUSH",
        "seq_num": seq_num,               # server-issued sequence number
        "stack_item_id": item_id,
        "item_type": item_type,          # SPELL | ABILITY | TRIGGER_ABILITY
        "source": source,
        "targets": targets,
        "controller": controller
    }

def trigger_order (seq_num, player_id, trigger_ids):
    return{
        "type": "TRIGGER_ORDER",
        "seq_num": seq_num,              # server-issued sequence number
        "player_id": player_id,
        "trigger_ids": trigger_ids  # player must order these
}

def trigger_order_response(ordered_trigger_ids):
    return{
        "type":                "TRIGGER_ORDER_RESPONSE",
        "seq_num":             15,   # must match the corresponding TRIGGER_ORDER seq_num
        "ordered_trigger_ids": ordered_trigger_ids
        # trg_04 placed first (resolves last); trg_03 on top (resolves first)
    }

def trigger_choice(trigger_id, source_id, effect_summary, legal_targets, requires_target):
    return{
        "type":             "TRIGGER_CHOICE",
        "seq_num":          20,           # server-issued sequence number
        "trigger_id": trigger_id,
        "source_id": source_id,
        "effect_summary": effect_summary,
        "requires_target": requires_target,         # true if player must also pick a target
        "legal_targets": legal_targets     # populated when requires_target is true;
                                     # elements are player_id strings or permanent id strings
    }

def trigger_choice_response(trigger_id, accept, chosen_target):
    return{
        "type":          "TRIGGER_CHOICE_RESPONSE",
        "seq_num":       20,              # must match the corresponding TRIGGER_CHOICE seq_num
        "trigger_id":    trigger_id,
        "accept":        accept,
        "chosen_target": chosen_target              # non-null only when accept=true AND requires_target=true;
                                     # absent or null when accept=false or requires_target=false
    }

def stack_resolve(item_id, result, state_changes):
    return{
        "type":          "STACK_RESOLVE",
        "seq_num":       31,              # server-issued sequence number
        "stack_item_id": item_id,
        "result":        result,       # RESOLVED | FIZZLE
        "state_changes": state_changes
    }

def declare_attackers(attackers):
    return{
        "type":      "DECLARE_ATTACKERS",
        "seq_num":   22,
        "attackers": attackers # send empty attackers array to declare no attack
    }

def declare_blockers(blockers):
    return{
        "type":     "DECLARE_BLOCKERS",
        "seq_num":  24,
        "blockers": blockers # send empty blockers array to not block
    }
    
def assign_damage_order(attacker_id, blocker_order):
    return{
        "type":         "ASSIGN_DAMAGE_ORDER",
        "seq_num":      26,
        "attacker_id":  attacker_id,
        "blocker_order": blocker_order # damage assigned to wall first, overflow goes to bears
    }

def combat_damage_result(damage_events, life_totals, creatures_died):
    return{
        "type": "COMBAT_DAMAGE_RESULT",
        "seq_num":        27,             # server-issued sequence number
        "damage_events": damage_events,
        "life_totals": life_totals,
        "creatures_died": creatures_died
    }

def play_land(card_id, seq_num):
    return{
        "type":    "PLAY_LAND",
        "seq_num": seq_num,
        "card_id": card_id # does not use the stack; one land play permitted per turn
    }

def discard(card_ids, seq_num):
    return{
        "type":     "DISCARD",
        "seq_num":  seq_num,
        "card_ids": card_ids # sent at cleanup when hand size exceeds 7
}

def concede(player_id, seq_num):
    return{
        "type":      "CONCEDE",
        "seq_num":   seq_num,
        "player_id": player_id
    }

def game_over(winner_id, loser_id, reason):
    return{
        "type":      "GAME_OVER",
        "seq_num":   100,             # server-issued sequence number
        "winner_id": winner_id,
        "loser_id":  loser_id,
        "reason":    reason # reason: LIFE_ZERO | DECK_EMPTY | CONCEDE | DISCONNECT
    }

def error(code, message, rejected_action):
    return{
        "type":            "ERROR",
        "seq_num":         14,            # echoes the seq_num of the rejected action when available
        "code":            code,
        "message":         message,
        "rejected_action": rejected_action
    }

def ping(timestamp, seq_num):
    return{
        "type":      "PING",
        "seq_num":   seq_num,              # used to correlate with PONG response
        "timestamp": timestamp   # Unix epoch milliseconds
    }

def pong(timestamp, seq_num):
    return{
  "type":      "PONG",
  "seq_num":   seq_num,              # echoes the PING seq_num
  "timestamp": timestamp   # echoes the PING timestamp
}
