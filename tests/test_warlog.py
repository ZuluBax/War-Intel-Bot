from __future__ import annotations

from app.war.warlog import (
    roster_join_event,
    roster_left_event,
    state_change_event,
    war_finished_event,
    war_registered_event,
)


def test_war_registered_event_names_the_enemy() -> None:
    event = war_registered_event(1, enemy_faction_name="Rival Crew")
    assert event.war_id == 1
    assert event.kind == "WAR_REGISTERED"
    assert "Rival Crew" in event.message


def test_war_registered_event_falls_back_when_enemy_unknown() -> None:
    event = war_registered_event(1, enemy_faction_name=None)
    assert "unknown faction" in event.message


def test_state_change_event_reads_naturally() -> None:
    event = state_change_event(1, was="WAR_DETECTED", now="WAR_ACTIVE")
    assert event.kind == "STATE_CHANGE"
    assert event.message == "War state changed: WAR DETECTED → WAR ACTIVE."


def test_war_finished_event_labels_win_loss_draw() -> None:
    assert "won" in war_finished_event(1, result="WIN").message
    assert "lost" in war_finished_event(1, result="LOSS").message
    assert "drawn" in war_finished_event(1, result="DRAW").message
    assert "ended" in war_finished_event(1, result=None).message


def test_roster_join_and_left_events_prefer_name_over_id() -> None:
    joined = roster_join_event(1, player_id=555, name="Enemy555")
    left = roster_left_event(1, player_id=555, name=None)
    assert joined.kind == "ROSTER_JOIN"
    assert "Enemy555" in joined.message
    assert left.kind == "ROSTER_LEFT"
    assert "Player 555" in left.message
