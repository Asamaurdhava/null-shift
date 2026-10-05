import time
import pytest
import game
from game import Room, REACTOR_ID, SHIFT_DURATION


def ready_room():
    room=Room('TEST')
    a=room.add_player('Alpha')
    b=room.add_player('Bravo')
    room.start(a.id)
    room.countdown_ends_at=time.time()-0.01
    room.tick()
    assert room.phase=='MATCH'
    return room,a,b


def test_room_start_requires_two_players():
    room=Room('TEST')
    a=room.add_player('Solo')
    with pytest.raises(ValueError): room.start(a.id)


def test_capture_is_authoritative_and_costs_energy():
    room,a,b=ready_room()
    before=a.energy
    room.capture(a.id,0)
    assert room.nodes[0].owner==a.id
    assert a.energy<before
    with pytest.raises(ValueError): room.capture(a.id,0)
    room.capture(b.id,0)
    assert room.nodes[0].owner==b.id


def test_shield_blocks_enemy_capture():
    room,a,b=ready_room()
    room.capture(a.id,1)
    a.energy=100
    room.use_power(a.id,'shield',1)
    b.energy=100
    with pytest.raises(ValueError, match='shielded'):
        room.capture(b.id,1)


def test_overload_neutralizes_enemy_node():
    room,a,b=ready_room()
    room.capture(a.id,2)
    b.energy=100
    room.use_power(b.id,'overload',2)
    assert room.nodes[2].owner is None


def test_jam_hits_opponent_and_blocks_action():
    room,a,b=ready_room()
    for i in range(3): room.capture(b.id,i)
    a.energy=100
    room.use_power(a.id,'jam')
    assert b.jammed_until>time.time()
    with pytest.raises(ValueError, match='jammed'):
        room.capture(b.id,6)


def test_reactor_bonus_regen_and_secret_privacy():
    room,a,b=ready_room()
    room.capture(a.id,REACTOR_ID)
    a.energy=30
    before_score=a.score
    tick_at=time.time()
    room.last_tick=tick_at-1
    room.tick(tick_at)
    assert a.energy>32
    assert a.score>=before_score+3
    public_a=room.public_state(a.id)
    public_b=room.public_state(b.id)
    pa=next(p for p in public_a['players'] if p['id']==a.id)
    pa_seen_by_b=next(p for p in public_b['players'] if p['id']==a.id)
    assert pa['secret'] is not None
    assert pa_seen_by_b['secret'] is None


def test_finish_orders_results_and_applies_secret_bonus():
    room,a,b=ready_room()
    a.secret={'key':'REACTOR','text':'Own reactor','bonus':250}
    a.energy=100
    room.nodes[REACTOR_ID].owner=a.id
    room.finish()
    assert room.phase=='RESULTS'
    ar=next(r for r in room.results if r['id']==a.id)
    assert ar['secretCompleted'] is True
    assert ar['secretBonus']==250


def test_reconnect_token_required():
    room=Room('TEST')
    a=room.add_player('Alpha')
    room.disconnect(a.id)
    with pytest.raises(ValueError): room.reconnect(a.id,'wrong')
    room.reconnect(a.id,a.token)
    assert a.connected


def test_rematch_only_allowed_after_results():
    room,a,b=ready_room()
    with pytest.raises(ValueError, match='after results'):
        room.rematch(a.id)
    room.finish()
    room.rematch(a.id)
    assert room.phase=='LOBBY'
    assert room.ends_at==0
    assert a.secret=={}


def test_forced_shift_uses_action_timestamp():
    room,a,b=ready_room()
    a.energy=100
    action_at=12345.0
    room.use_power(a.id,'chaos',t=action_at)
    assert room.shift is not None
    assert room.shift['until']==action_at+SHIFT_DURATION


def test_inversion_transfers_active_shield_with_node(monkeypatch):
    room,a,b=ready_room()
    room.capture(a.id,1)
    a.energy=100
    shield_at=time.time()
    room.use_power(a.id,'shield',1,t=shield_at)
    monkeypatch.setattr(game.random,'choice',lambda choices:'INVERSION')
    room.trigger_shift(t=shield_at+1)
    assert room.nodes[1].owner==b.id
    assert room.nodes[1].shielded_by==b.id


def test_storage_roundtrip_preserves_authoritative_state():
    room = Room("ABCD")
    a = room.add_player("Alpha", session_id="session-a")
    b = room.add_player("Bravo", session_id="session-b")
    room.start(a.id)
    room.countdown_ends_at = 100.0
    room.begin_match_if_due(100.0)
    room.capture(a.id, 0, t=100.1)
    room.use_power(a.id, "shield", 0, t=100.2)
    room.players[b.id].jammed_until = 109.0
    room.shift = {"type": "CHAIN", "until": 110.0}
    room.final_collapse = True

    restored = Room.from_storage(room.to_storage())

    assert restored.code == room.code
    assert restored.phase == room.phase
    assert restored.host_id == a.id
    assert restored.players[a.id].token == a.token
    assert restored.players[a.id].session_id == "session-a"
    assert restored.players[b.id].jammed_until == 109.0
    assert restored.nodes[0].owner == a.id
    assert restored.nodes[0].shielded_by == a.id
    assert restored.shift == room.shift
    assert restored.final_collapse is True
    assert restored.players[a.id].secret == room.players[a.id].secret


def test_stale_session_disconnect_does_not_disconnect_replacement():
    room = Room("ABCD")
    player = room.add_player("Alpha", session_id="old-session")
    room.reconnect(player.id, player.token, session_id="new-session")

    room.disconnect(player.id, session_id="old-session")

    assert room.players[player.id].connected is True
    assert room.players[player.id].session_id == "new-session"
