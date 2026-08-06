from __future__ import annotations

import time

import pytest

from app import state
from app.monitor import FatalHsmError
from app.poller import ClientsPoller, HsmMonitor, RolesPoller


def setup_function() -> None:
    state._reset_for_tests()


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


@pytest.fixture
def fake_roles_poller(monkeypatch):
    calls = []
    should_fail = {"value": False}

    def fake_poll_once(entry, global_config, expectations, username, password):
        calls.append(time.monotonic())
        if should_fail["value"]:
            raise FatalHsmError("simulated connection failure")
        return {"name": entry["name"], "id": "42", "raw": {}, "partitions": [], "problems": []}

    monkeypatch.setattr("app.poller.poll_once", fake_poll_once)

    poller = RolesPoller({"name": "hsm-test.example"}, {}, {}, "user", "pass", interval=5)
    yield poller, calls, should_fail
    poller.stop()


def test_start_polls_immediately_without_waiting_the_interval(fake_roles_poller):
    poller, calls, _ = fake_roles_poller

    poller.start()
    _wait_until(lambda: len(calls) == 1)

    assert state.get("hsm-test.example")["thread_status"] == "running"


def test_check_now_polls_immediately_bypassing_the_interval(fake_roles_poller):
    poller, calls, _ = fake_roles_poller

    poller.start()
    _wait_until(lambda: len(calls) == 1)

    poller.check_now()
    _wait_until(lambda: len(calls) == 2)


def test_stop_halts_the_loop_promptly_and_it_stays_stopped(fake_roles_poller):
    poller, calls, _ = fake_roles_poller

    poller.start()
    _wait_until(lambda: len(calls) == 1)

    poller.stop()
    _wait_until(lambda: not poller.is_alive())
    assert state.get("hsm-test.example")["thread_status"] == "stopped"

    count_after_stop = len(calls)
    time.sleep(0.3)
    assert len(calls) == count_after_stop


def test_fatal_error_stops_the_thread_itself_and_records_a_fatal_problem(fake_roles_poller):
    poller, _calls, should_fail = fake_roles_poller
    should_fail["value"] = True

    poller.start()
    _wait_until(lambda: not poller.is_alive())

    entry = state.get("hsm-test.example")
    assert entry["thread_status"] == "stopped"
    assert entry["role_problems"][0]["severity"] == "fatal"
    assert entry["role_problems"][0]["kind"] == "connection_failed"


def test_check_now_resumes_a_fatally_stopped_poller(fake_roles_poller):
    poller, calls, should_fail = fake_roles_poller
    should_fail["value"] = True

    poller.start()
    _wait_until(lambda: not poller.is_alive())

    should_fail["value"] = False
    poller.check_now()
    _wait_until(lambda: poller.is_alive())

    assert state.get("hsm-test.example")["thread_status"] == "running"


@pytest.fixture
def fake_clients_poller(monkeypatch):
    calls = []
    should_fail = {"value": False}

    def fake_poll_clients_once(entry, global_config, username, password):
        calls.append(time.monotonic())
        if should_fail["value"]:
            raise FatalHsmError("simulated connection failure")
        return {"p1": ["laptop.example.com"]}

    monkeypatch.setattr("app.poller.poll_clients_once", fake_poll_clients_once)

    poller = ClientsPoller({"name": "hsm-test.example"}, {}, "user", "pass", interval=5)
    yield poller, calls, should_fail
    poller.stop()


def test_clients_poller_start_polls_immediately_and_writes_its_own_keys(fake_clients_poller):
    poller, calls, _ = fake_clients_poller

    poller.start()
    _wait_until(lambda: len(calls) == 1)

    entry = state.get("hsm-test.example")
    assert entry["clients_thread_status"] == "running"
    assert entry["partition_clients"] == {"p1": ["laptop.example.com"]}


def test_clients_poller_fatal_error_stops_itself_and_records_a_fatal_client_problem(fake_clients_poller):
    poller, _calls, should_fail = fake_clients_poller
    should_fail["value"] = True

    poller.start()
    _wait_until(lambda: not poller.is_alive())

    entry = state.get("hsm-test.example")
    assert entry["clients_thread_status"] == "stopped"
    assert entry["client_problems"][0]["severity"] == "fatal"
    assert entry["client_problems"][0]["kind"] == "connection_failed"


def test_hsm_monitor_start_stop_check_now_control_both_pollers(monkeypatch):
    roles_calls = []
    clients_calls = []

    def fake_poll_once(entry, global_config, expectations, username, password):
        roles_calls.append(1)
        return {"name": entry["name"], "id": "42", "raw": {}, "partitions": [], "problems": []}

    def fake_poll_clients_once(entry, global_config, username, password):
        clients_calls.append(1)
        return {}

    monkeypatch.setattr("app.poller.poll_once", fake_poll_once)
    monkeypatch.setattr("app.poller.poll_clients_once", fake_poll_clients_once)

    entry = {"name": "hsm-test.example"}
    roles = RolesPoller(entry, {}, {}, "user", "pass", interval=5)
    clients = ClientsPoller(entry, {}, "user", "pass", interval=5)
    monitor = HsmMonitor(roles, clients)

    monitor.start()
    _wait_until(lambda: roles.is_alive() and clients.is_alive())
    _wait_until(lambda: roles_calls and clients_calls)

    monitor.stop()
    _wait_until(lambda: not roles.is_alive() and not clients.is_alive())

    monitor.check_now()
    _wait_until(lambda: roles.is_alive() and clients.is_alive())

    monitor.stop()


def test_one_poller_going_fatal_does_not_affect_the_other(monkeypatch):
    # Regression guard: role_problems/client_problems must be separate cache keys -
    # if they shared one "problems" key, one poller's state.update() would silently
    # erase whatever the other poller had just written.
    def fake_poll_once(entry, global_config, expectations, username, password):
        return {"name": entry["name"], "id": "42", "raw": {}, "partitions": [], "problems": [{"kind": "role_mismatch"}]}

    def fake_poll_clients_once(entry, global_config, username, password):
        raise FatalHsmError("clients side failure")

    monkeypatch.setattr("app.poller.poll_once", fake_poll_once)
    monkeypatch.setattr("app.poller.poll_clients_once", fake_poll_clients_once)

    entry = {"name": "hsm-test.example"}
    roles = RolesPoller(entry, {}, {}, "user", "pass", interval=5)
    clients = ClientsPoller(entry, {}, "user", "pass", interval=5)

    roles.start()
    clients.start()
    _wait_until(lambda: not clients.is_alive())
    time.sleep(0.1)  # let the roles poller's first cycle land too

    assert roles.is_alive()
    snapshot = state.get("hsm-test.example")
    assert snapshot["thread_status"] == "running"
    assert snapshot["role_problems"] == [{"kind": "role_mismatch"}]
    assert snapshot["clients_thread_status"] == "stopped"
    assert snapshot["client_problems"][0]["severity"] == "fatal"

    roles.stop()
