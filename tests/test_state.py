from __future__ import annotations

from app import state


def setup_function() -> None:
    state._reset_for_tests()


def test_seed_gives_a_placeholder_that_get_and_get_all_can_see():
    state.seed("hsm-a")

    entry = state.get("hsm-a")
    assert entry["name"] == "hsm-a"
    assert entry["thread_status"] == "starting"
    assert entry["problems"] == []

    assert [e["name"] for e in state.get_all()] == ["hsm-a"]


def test_seed_does_not_clobber_an_existing_entry():
    state.update("hsm-a", {"name": "hsm-a", "thread_status": "running"})
    state.seed("hsm-a")

    assert state.get("hsm-a")["thread_status"] == "running"


def test_update_merges_fields_rather_than_replacing_the_entry():
    state.update("hsm-a", {"name": "hsm-a", "id": "1", "problems": []})
    state.update("hsm-a", {"problems": [{"severity": "fatal"}]})

    entry = state.get("hsm-a")
    assert entry["id"] == "1"
    assert entry["problems"] == [{"severity": "fatal"}]


def test_get_missing_name_returns_none():
    assert state.get("does-not-exist") is None


def test_get_and_get_all_return_copies_not_live_references():
    state.update("hsm-a", {"name": "hsm-a", "problems": []})

    snapshot = state.get("hsm-a")
    snapshot["problems"].append({"severity": "fatal"})

    # Mutating the dict returned by get() must not corrupt the shared cache -
    # both the overview and detail routes read via get()/get_all() concurrently
    # with poller threads writing via update().
    assert state.get("hsm-a")["problems"] == []
