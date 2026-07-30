from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.monitor import (
    FatalHsmError,
    _is_activated,
    _partition_role_status,
    _partition_status,
    _role_kind,
    poll_all,
    poll_once,
    role_expectations,
    suggest_partition_config,
)

CONFIG = {
    "global": {"verify": False, "timeout": 10, "api_version": 15},
    "hsms": [
        {"name": "good-hsm.example"},
        {"name": "bad-hsm.example"},
    ],
}


def _session_ctor(mock_session):
    def ctor(base_url, **_kwargs):
        session = mock_session.return_value
        session._name = base_url.split("//")[1].split(":")[0]
        session.__enter__.return_value = session
        session.__exit__.return_value = False  # never swallow the body's exception
        return session

    return ctor


@patch("app.monitor.LunaClient")
@patch("app.monitor.LunaSession")
def test_poll_all_isolates_one_fatally_failing_hsm_from_the_others(mock_session, mock_client, monkeypatch):
    monkeypatch.setenv("LUNA_USERNAME", "user")
    monkeypatch.setenv("LUNA_PASSWORD", "pass")

    def client_for(session):
        client = mock_client.return_value
        if session._name == "bad-hsm.example":
            client.get_hsm.side_effect = RuntimeError("500 Internal Server Error")
        else:
            client.get_hsm.return_value = {"id": "42"}  # "id" doubles as the serial on this appliance
            client.list_partitions.return_value = [{"id": "p1"}]
            client.list_roles.return_value = []
        return client

    mock_session.side_effect = _session_ctor(mock_session)
    mock_client.side_effect = client_for

    results = poll_all(CONFIG)
    assert len(results) == 2

    good, bad = results
    assert good["name"] == "good-hsm.example"
    assert good["id"] == "42"
    assert good["problems"] == []
    assert len(good["partitions"]) == 1

    assert bad["name"] == "bad-hsm.example"
    assert bad["id"] is None
    assert bad["partitions"] == []
    assert len(bad["problems"]) == 1
    assert bad["problems"][0]["severity"] == "fatal"
    assert bad["problems"][0]["kind"] == "connection_failed"
    assert "500" in bad["problems"][0]["message"]


@patch("app.monitor.LunaClient")
@patch("app.monitor.LunaSession")
def test_poll_once_raises_fatal_on_connection_failure(mock_session, mock_client):
    mock_session.side_effect = RuntimeError("connection refused")

    with pytest.raises(FatalHsmError, match="connection refused"):
        poll_once({"name": "dead.example"}, {}, {}, "user", "pass")


@patch("app.monitor.LunaClient")
@patch("app.monitor.LunaSession")
def test_poll_once_refuses_a_reserved_username_before_any_network_call(mock_session, mock_client):
    # Regression guard: a misconfigured LUNA_USERNAME (e.g. the .env duplicate-key bug
    # that once left LUNA_USERNAME=admin active) must never reach a real login attempt.
    with pytest.raises(FatalHsmError, match="admin"):
        poll_once({"name": "hsm.example"}, {}, {}, "admin", "whatever")

    mock_session.assert_not_called()
    mock_client.assert_not_called()


@patch("app.monitor.LunaClient")
@patch("app.monitor.LunaSession")
def test_poll_once_reports_role_mismatch_and_partition_fetch_error_as_problems(mock_session, mock_client):
    session = mock_session.return_value
    session.__enter__.return_value = session
    session.__exit__.return_value = False

    client = mock_client.return_value
    client.get_hsm.return_value = {"id": "42"}
    client.list_partitions.return_value = [{"id": "p-ok"}, {"id": "p-broken"}]

    def list_roles(hsm_id, partition_id):
        if partition_id == "p-broken":
            raise RuntimeError("403 forbidden")
        return [{"id": "cu", "url": "/roles/cu"}, {"id": "co", "url": "/roles/co"}]

    role_details = {
        "cu": {"id": "cu", "activated": True},
        "co": {"id": "co", "activated": True},
    }

    client.list_roles.side_effect = list_roles
    client.get_role.side_effect = lambda url: role_details[url.rsplit("/", 1)[-1]]

    # p-ok's CO should be deactivated per config, but the appliance reports it activated.
    expectations = {"p-ok": {"cu": "activated", "co": "deactivated"}}

    result = poll_once({"name": "hsm.example"}, {}, expectations, "user", "pass")

    kinds = {p["kind"] for p in result["problems"]}
    assert kinds == {"role_mismatch", "partition_fetch_error"}

    mismatch = next(p for p in result["problems"] if p["kind"] == "role_mismatch")
    assert mismatch["severity"] == "error"
    assert mismatch["role"] == "co"
    assert mismatch["partition"] == "p-ok"
    assert "should be deactivated" in mismatch["message"]

    fetch_error = next(p for p in result["problems"] if p["kind"] == "partition_fetch_error")
    assert fetch_error["severity"] == "error"
    assert fetch_error["partition"] == "p-broken"
    assert "403 forbidden" in fetch_error["message"]


def test_role_expectations_resolves_partition_to_template_case_insensitively():
    config = {
        "partition_templates": [
            {"name": "secure", "cu": "activated", "co": "deactivated"},
            {"name": "legacy", "cu": "activated", "co": "activated"},
        ],
        "partitions": [{"name": "DSS", "template": "Legacy"}],
    }

    expectations = role_expectations(config)

    assert expectations == {"dss": {"name": "legacy", "cu": "activated", "co": "activated"}}


def test_role_expectations_skips_partitions_pointing_at_an_unknown_template():
    config = {
        "partition_templates": [{"name": "legacy", "cu": "activated", "co": "activated"}],
        "partitions": [{"name": "dss", "template": "does-not-exist"}],
    }

    assert role_expectations(config) == {}


def test_role_expectations_defaults_to_empty_without_either_section():
    assert role_expectations({}) == {}


def test_role_kind_reads_the_id_field_like_the_nautobot_plugin_did():
    # role['id'] is literally "cu"/"co"/"so" - confirmed from fetch_hsm_data.py and the
    # diffsync adapter in the old nautobot plugin, which worked against this appliance
    # under API v12. "so" (partition SO / master role) isn't one we track.
    assert _role_kind({"id": "cu"}) == "cu"
    assert _role_kind({"id": "co"}) == "co"
    assert _role_kind({"id": "so"}) is None
    assert _role_kind({"id": "CU"}) == "cu"  # case-insensitive, just in case
    assert _role_kind({}) is None


def test_is_activated_reads_the_activated_field():
    assert _is_activated({"activated": True}) is True
    assert _is_activated({"activated": False}) is False
    assert _is_activated({"unrelated": "value"}) is None


def test_partition_role_status_flags_actual_vs_expected_per_role():
    roles = [{"id": "cu", "activated": True}, {"id": "co", "activated": True}]
    template = {"cu": "activated", "co": "deactivated"}

    status = _partition_role_status(roles, template)

    assert status == {
        "cu": {"actual": True, "expected": True},
        "co": {"actual": True, "expected": False},
    }


def test_partition_status_isolates_one_bad_partition_from_its_siblings():
    client = MagicMock()
    client.list_roles.side_effect = [RuntimeError("403 forbidden"), []]

    partitions = [{"id": "broken"}, {"id": "fine"}]
    for partition in partitions:
        _partition_status(client, "hsm-1", partition, role_expectations={})

    broken, fine = partitions
    assert broken["error"] == "403 forbidden"
    assert broken["role_status"] == {
        "cu": {"actual": None, "expected": None},
        "co": {"actual": None, "expected": None},
    }
    assert fine["error"] is None


def test_suggest_partition_config_matches_existing_templates_by_observed_state():
    config = {
        "partition_templates": [
            {"name": "secure", "cu": "activated", "co": "deactivated"},
            {"name": "legacy", "cu": "activated", "co": "activated"},
        ],
    }
    hsm = {
        "partitions": [
            {"label": "TSA2018", "role_status": {"cu": {"actual": True}, "co": {"actual": True}}},
            {"label": "IK2016", "role_status": {"cu": {"actual": True}, "co": {"actual": False}}},
        ]
    }

    output = suggest_partition_config(config, hsm)

    assert output == (
        "partitions:\n"
        "  - name: TSA2018\n"
        "    template: legacy\n"
        "  - name: IK2016\n"
        "    template: secure\n"
    )


def test_suggest_partition_config_never_invents_a_template_it_flags_unmatched_instead():
    config = {"partition_templates": [{"name": "legacy", "cu": "activated", "co": "activated"}]}

    # Doesn't match "legacy" (co differs), and one role is still unknown (None).
    hsm = {
        "partitions": [
            {"label": "WEIRD", "role_status": {"cu": {"actual": False}, "co": {"actual": False}}},
            {"label": "UNKNOWN", "role_status": {"cu": {"actual": None}, "co": {"actual": True}}},
        ]
    }

    output = suggest_partition_config(config, hsm)

    assert "template:" not in output
    assert "no matching template for WEIRD (cu=False, co=False)" in output
    assert "no matching template for UNKNOWN (cu=None, co=True)" in output
