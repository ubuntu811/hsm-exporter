from __future__ import annotations

from app import state


def setup_function() -> None:
    state._reset_for_tests()


def test_seed_gives_a_placeholder_that_get_and_get_all_can_see():
    state.seed("hsm-a")

    entry = state.get("hsm-a")
    assert entry["name"] == "hsm-a"
    assert entry["thread_status"] == "starting"
    assert entry["clients_thread_status"] == "starting"
    assert entry["role_problems"] == []
    assert entry["client_problems"] == []
    assert entry["log"] == []

    assert [e["name"] for e in state.get_all()] == ["hsm-a"]


def test_seed_does_not_clobber_an_existing_entry():
    state.update("hsm-a", {"name": "hsm-a", "thread_status": "running"})
    state.seed("hsm-a")

    assert state.get("hsm-a")["thread_status"] == "running"


def test_update_merges_fields_rather_than_replacing_the_entry():
    state.update("hsm-a", {"name": "hsm-a", "id": "1", "role_problems": []})
    state.update("hsm-a", {"role_problems": [{"severity": "fatal"}]})

    entry = state.get("hsm-a")
    assert entry["id"] == "1"
    assert entry["role_problems"] == [{"severity": "fatal"}]


def test_update_touching_one_key_does_not_affect_a_different_key():
    # This is the actual clobbering bug it guards against: two independent pollers
    # calling update() with different fields on the same entry must not stomp on
    # each other, since dict.update() only ever touches the keys it's given.
    state.update("hsm-a", {"name": "hsm-a", "role_problems": ["from roles poller"]})
    state.update("hsm-a", {"client_problems": ["from clients poller"]})

    entry = state.get("hsm-a")
    assert entry["role_problems"] == ["from roles poller"]
    assert entry["client_problems"] == ["from clients poller"]


def test_get_missing_name_returns_none():
    assert state.get("does-not-exist") is None


def test_get_and_get_all_return_copies_not_live_references():
    state.update("hsm-a", {"name": "hsm-a", "role_problems": []})

    snapshot = state.get("hsm-a")
    snapshot["role_problems"].append({"severity": "fatal"})

    # Mutating the dict returned by get() must not corrupt the shared cache -
    # both the overview and detail routes read via get()/get_all() concurrently
    # with poller threads writing via update().
    assert state.get("hsm-a")["role_problems"] == []


def test_log_event_appends_in_order_with_a_timestamp():
    state.log_event("hsm-a", "first")
    state.log_event("hsm-a", "second")

    log = state.get("hsm-a")["log"]
    assert [entry["message"] for entry in log] == ["first", "second"]
    assert all("timestamp" in entry for entry in log)


def test_log_event_works_even_if_the_hsm_was_never_seeded():
    # The setup flow can log_event() for an HSM before its poller has run a single
    # cycle - must not KeyError/crash just because seed() or update() never ran first.
    state.log_event("brand-new-hsm", "hello")

    assert state.get("brand-new-hsm")["log"][0]["message"] == "hello"


def test_log_event_trims_to_the_max_entry_cap():
    original_cap = state.LOG_MAX_ENTRIES
    state.LOG_MAX_ENTRIES = 3
    try:
        for i in range(5):
            state.log_event("hsm-a", f"entry {i}")

        log = state.get("hsm-a")["log"]
        assert [entry["message"] for entry in log] == ["entry 2", "entry 3", "entry 4"]
    finally:
        state.LOG_MAX_ENTRIES = original_cap


def test_log_event_does_not_clobber_fields_written_by_update():
    # Same clobbering concern as role_problems/client_problems, but for log_event()
    # specifically since it does its own read-modify-write rather than going through
    # update()'s plain dict.update().
    state.update("hsm-a", {"name": "hsm-a", "id": "42"})
    state.log_event("hsm-a", "something happened")

    entry = state.get("hsm-a")
    assert entry["id"] == "42"
    assert entry["log"][0]["message"] == "something happened"
