from __future__ import annotations

import time

import pytest

from app import state
from app.monitor import FatalHsmError
from app.poller import Poller


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
def fake_poller(monkeypatch):
    calls = []
    should_fail = {"value": False}

    def fake_poll_once(entry, global_config, expectations, username, password):
        calls.append(time.monotonic())
        if should_fail["value"]:
            raise FatalHsmError("simulated connection failure")
        return {"name": entry["name"], "id": "42", "raw": {}, "partitions": [], "problems": []}

    monkeypatch.setattr("app.poller.poll_once", fake_poll_once)

    poller = Poller({"name": "hsm-test.example"}, {}, {}, "user", "pass", interval=5)
    yield poller, calls, should_fail
    poller.stop()


def test_start_polls_immediately_without_waiting_the_interval(fake_poller):
    poller, calls, _ = fake_poller

    poller.start()
    _wait_until(lambda: len(calls) == 1)

    assert state.get("hsm-test.example")["thread_status"] == "running"


def test_check_now_polls_immediately_bypassing_the_interval(fake_poller):
    poller, calls, _ = fake_poller

    poller.start()
    _wait_until(lambda: len(calls) == 1)

    poller.check_now()
    _wait_until(lambda: len(calls) == 2)


def test_stop_halts_the_loop_promptly_and_it_stays_stopped(fake_poller):
    poller, calls, _ = fake_poller

    poller.start()
    _wait_until(lambda: len(calls) == 1)

    poller.stop()
    _wait_until(lambda: not poller.is_alive())
    assert state.get("hsm-test.example")["thread_status"] == "stopped"

    count_after_stop = len(calls)
    time.sleep(0.3)
    assert len(calls) == count_after_stop


def test_fatal_error_stops_the_thread_itself_and_records_a_fatal_problem(fake_poller):
    poller, _calls, should_fail = fake_poller
    should_fail["value"] = True

    poller.start()
    _wait_until(lambda: not poller.is_alive())

    entry = state.get("hsm-test.example")
    assert entry["thread_status"] == "stopped"
    assert entry["problems"][0]["severity"] == "fatal"
    assert entry["problems"][0]["kind"] == "connection_failed"


def test_check_now_resumes_a_fatally_stopped_poller(fake_poller):
    poller, calls, should_fail = fake_poller
    should_fail["value"] = True

    poller.start()
    _wait_until(lambda: not poller.is_alive())

    should_fail["value"] = False
    poller.check_now()
    _wait_until(lambda: poller.is_alive())

    assert state.get("hsm-test.example")["thread_status"] == "running"
