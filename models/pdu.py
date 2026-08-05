def player_ready(player_id, deck, seq_num):
    return {
        "type": "PLAYER_READY",
        "seq_num": seq_num,
        "player_id": player_id,
        "deck_list": deck
    }

def game_state_update(state, seq_num):
    return {
        "type": "GAME_STATE_UPDATE",
        "seq_num": seq_num,
        "state": state
    }

def mulligan_choice(keep, cards_bottom, seq_num):
    return {
        "type": "MULLIGAN_CHOICE",
        "seq_num": seq_num, 
        "keep": keep,
        "cards_to_bottom": cards_bottom
    }

def phase_transition(from_phase, to_phase, active_player, seq_num, turn):
    return {
        "type": "PHASE_TRANSITION",
        "seq_num": seq_num,
        "from_phase": from_phase,
        "to_phase": to_phase,
        "active_player": active_player,
        "turn": turn
    }
   
def priority_grant(player_id, seq_num, time_limit):
    return {
        "type": "PRIORITY_GRANT",
        "player_id": player_id,
        "seq_num": seq_num,
        "time_limit_ms": time_limit
    }

def priority_pass(seq_num):
    return {
        "type": "PRIORITY_PASS",
        "seq_num": seq_num
    }

# Backward compatibility alias
priotity_pass = priority_pass

def cast_spell(card_id, targets, mana_payment, seq_num):
    return {
        "type": "CAST_SPELL",
        "seq_num": seq_num,
        "card_id": card_id,
        "targets": targets,
        "mana_payment": mana_payment
    }

def activate_ability(source, ability_index, targets, seq_num, cost_payment):
    return {
        "type": "ACTIVATE_ABILITY",
        "seq_num": seq_num,
        "source_id": source,
        "ability_index": ability_index,
        "targets": targets,
        "cost_payment": cost_payment
    }

def stack_push(item_id, item_type, source, targets, seq_num, controller):
    return {
        "type": "STACK_PUSH",
        "seq_num": seq_num,
        "stack_item_id": item_id,
        "item_type": item_type,
        "source": source,
        "targets": targets,
        "controller": controller
    }

def trigger_order(seq_num, player_id, trigger_ids):
    return {
        "type": "TRIGGER_ORDER",
        "seq_num": seq_num,
        "player_id": player_id,
        "trigger_ids": trigger_ids
    }

def trigger_order_response(ordered_trigger_ids, seq_num):
    return {
        "type": "TRIGGER_ORDER_RESPONSE",
        "seq_num": seq_num,
        "ordered_trigger_ids": ordered_trigger_ids
    }

def trigger_choice(trigger_id, source_id, effect_summary, legal_targets, requires_target, seq_num):
    return {
        "type": "TRIGGER_CHOICE",
        "seq_num": seq_num,
        "trigger_id": trigger_id,
        "source_id": source_id,
        "effect_summary": effect_summary,
        "requires_target": requires_target,
        "legal_targets": legal_targets
    }

def trigger_choice_response(trigger_id, accept, chosen_target, seq_num):
    return {
        "type": "TRIGGER_CHOICE_RESPONSE",
        "seq_num": seq_num,
        "trigger_id": trigger_id,
        "accept": accept,
        "chosen_target": chosen_target
    }

def stack_resolve(item_id, result, state_changes, seq_num):
    return {
        "type": "STACK_RESOLVE",
        "seq_num": seq_num,
        "stack_item_id": item_id,
        "result": result,
        "state_changes": state_changes
    }

def declare_attackers(attackers, seq_num):
    return {
        "type": "DECLARE_ATTACKERS",
        "seq_num": seq_num,
        "attackers": attackers
    }

def declare_blockers(blockers, seq_num):
    return {
        "type": "DECLARE_BLOCKERS",
        "seq_num": seq_num,
        "blockers": blockers
    }
    
def assign_damage_order(attacker_id, blocker_order, seq_num):
    return {
        "type": "ASSIGN_DAMAGE_ORDER",
        "seq_num": seq_num,
        "attacker_id": attacker_id,
        "blocker_order": blocker_order
    }

def combat_damage_result(damage_events, life_totals, creatures_died, seq_num):
    return {
        "type": "COMBAT_DAMAGE_RESULT",
        "seq_num": seq_num,
        "damage_events": damage_events,
        "life_totals": life_totals,
        "creatures_died": creatures_died
    }

def play_land(card_id, seq_num):
    return {
        "type": "PLAY_LAND",
        "seq_num": seq_num,
        "card_id": card_id
    }

def discard(card_ids, seq_num):
    return {
        "type": "DISCARD",
        "seq_num": seq_num,
        "card_ids": card_ids
    }

def concede(player_id, seq_num):
    return {
        "type": "CONCEDE",
        "seq_num": seq_num,
        "player_id": player_id
    }

def game_over(winner_id, loser_id, reason, seq_num=100):
    return {
        "type": "GAME_OVER",
        "seq_num": seq_num,
        "winner_id": winner_id,
        "loser_id": loser_id,
        "reason": reason
    }

def error(code, message, rejected_action, seq_num):
    return {
        "type": "ERROR",
        "seq_num": seq_num,
        "code": code,
        "message": message,
        "rejected_action": rejected_action
    }

def ping(timestamp, seq_num):
    return {
        "type": "PING",
        "seq_num": seq_num,
        "timestamp": timestamp
    }

def pong(timestamp, seq_num):
    return {
        "type": "PONG",
        "seq_num": seq_num,
        "timestamp": timestamp
    }