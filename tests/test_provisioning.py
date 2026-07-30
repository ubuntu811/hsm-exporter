from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.luna.provisioning import MONITOR_ROLE_ACL, RESERVED_USERNAMES, provision_monitor_user

SESSION_KWARGS = {"base_url": "https://hsm.example:8443", "verify": False, "timeout": 10, "api_version": 15}


def _admin_session(roles: list[dict], users: list[dict]) -> MagicMock:
    session = MagicMock()
    session.api_version = 15
    session.get_json.side_effect = lambda path: (
        {"roles": roles} if path == "/roles" else {"users": users}
    )
    return session


def _self_session_mock(mock_luna_session: MagicMock) -> MagicMock:
    self_session = MagicMock()
    self_session.__enter__.return_value = self_session
    self_session.__exit__.return_value = False
    mock_luna_session.return_value = self_session
    return self_session


@patch("app.luna.provisioning.LunaSession")
def test_fresh_role_and_user_are_created_then_password_set_via_self_login(mock_luna_session):
    admin_session = _admin_session(roles=[], users=[])
    self_session = _self_session_mock(mock_luna_session)

    steps = provision_monitor_user(admin_session, "monitor", "final-pass", SESSION_KWARGS)

    admin_session.post.assert_any_call(
        "/roles",
        payload=json.dumps({"roleId": "monitor", "fullName": "HSM Monitor Readonly Role"}),
        expected_status_codes=(204,),
    )
    admin_session.put.assert_any_call(
        "/roles/monitor/resources",
        payload=MONITOR_ROLE_ACL,
        headers={
            "Content-Type": "application/vnd.safenetinc.lunasa+octet-stream;version=15",
            "Accept": "application/octet-stream",
        },
        expected_status_codes=(202, 204),
    )

    admin_session.delete.assert_not_called()  # nothing to delete, user didn't exist yet

    create_call = next(c for c in admin_session.post.call_args_list if c.args[0] == "/users")
    created_payload = json.loads(create_call.kwargs["payload"])
    assert created_payload["userId"] == "monitor"
    temp_password = created_payload["password"]
    assert temp_password != "final-pass"  # the admin-set password must never be the real one

    # self-login uses that exact temp password, against the same appliance
    mock_luna_session.assert_called_once_with(username="monitor", password=temp_password, **SESSION_KWARGS)

    # the self-authenticated session is what sets the FINAL password, via the documented
    # changePassword action (self-scoped by the framework, unlike a raw PUT /users/{id})
    self_session.post.assert_called_once()
    self_args, self_kwargs = self_session.post.call_args
    assert self_args[0] == "/users/monitor/actions/changePassword"
    change_payload = json.loads(self_kwargs["payload"])
    assert change_payload == {"currentPassword": temp_password, "password": "final-pass"}
    assert self_kwargs["expected_status_codes"] == (204,)

    assert any("created user" in s for s in steps)
    assert any("set its password" in s for s in steps)


@patch("app.luna.provisioning.LunaSession")
def test_existing_role_is_kept_but_existing_user_is_deleted_and_recreated(mock_luna_session):
    admin_session = _admin_session(roles=[{"id": "monitor"}], users=[{"id": "monitor"}])
    self_session = _self_session_mock(mock_luna_session)

    steps = provision_monitor_user(admin_session, "monitor", "final-pass", SESSION_KWARGS)

    assert not any(c.args[0] == "/roles" for c in admin_session.post.call_args_list)
    # ACL still applied even though the role already existed - regression guard for the
    # old nautobot plugin bug where a fresh role got zero permissions until run twice.
    admin_session.put.assert_any_call(
        "/roles/monitor/resources",
        payload=MONITOR_ROLE_ACL,
        headers={
            "Content-Type": "application/vnd.safenetinc.lunasa+octet-stream;version=15",
            "Accept": "application/octet-stream",
        },
        expected_status_codes=(202, 204),
    )

    admin_session.delete.assert_called_once_with("/users/monitor", expected_status_codes=(204,))
    assert any(c.args[0] == "/users" for c in admin_session.post.call_args_list)

    self_session.post.assert_called_once()
    assert self_session.post.call_args.args[0] == "/users/monitor/actions/changePassword"
    assert json.loads(self_session.post.call_args.kwargs["payload"])["password"] == "final-pass"

    assert any("role 'monitor' already exists" in s for s in steps)
    assert any("deleted existing user" in s for s in steps)


@pytest.mark.parametrize("username", sorted(RESERVED_USERNAMES))
def test_refuses_to_touch_builtin_appliance_accounts(username):
    admin_session = MagicMock()

    with pytest.raises(ValueError, match=username):
        provision_monitor_user(admin_session, username, "whatever", SESSION_KWARGS)

    # Must fail before making a single appliance call.
    admin_session.get_json.assert_not_called()
    admin_session.post.assert_not_called()
    admin_session.put.assert_not_called()
    admin_session.delete.assert_not_called()
