from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.luna.provisioning import MONITOR_ROLE_ACL, RESERVED_USERNAMES, get_role_acl, provision_monitor_user

SESSION_KWARGS = {"base_url": "https://hsm.example:8443", "verify": False, "timeout": 10, "api_version": 15}


def _admin_session(roles: list[dict], users: list[dict], live_acl: str = MONITOR_ROLE_ACL) -> MagicMock:
    session = MagicMock()
    session.api_version = 15
    session.get_json.side_effect = lambda path: (
        {"roles": roles} if path == "/roles" else {"users": users}
    )
    session.get.return_value = MagicMock(text=live_acl)
    return session


def _self_session_mock(mock_luna_session: MagicMock) -> MagicMock:
    self_session = MagicMock()
    self_session.__enter__.return_value = self_session
    self_session.__exit__.return_value = False
    mock_luna_session.return_value = self_session
    return self_session


@patch("app.luna.provisioning.LunaSession")
def test_fresh_role_and_user_are_created_then_password_set_via_self_login(mock_luna_session):
    # "mon", not "monitor" - "monitor" is itself one of the appliance's reserved
    # built-in roles (see test_refuses_to_touch_builtin_appliance_accounts below),
    # so it can't be used as a test username here without immediately hitting that
    # guard instead of exercising this code path.
    admin_session = _admin_session(roles=[], users=[])
    self_session = _self_session_mock(mock_luna_session)

    steps = provision_monitor_user(admin_session, "mon", "final-pass", SESSION_KWARGS)

    admin_session.post.assert_any_call(
        "/roles",
        payload=json.dumps({"roleId": "mon", "fullName": "HSM Monitor Readonly Role"}),
        expected_status_codes=(204,),
    )
    admin_session.put.assert_any_call(
        "/roles/mon/resources",
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
    assert created_payload["userId"] == "mon"
    temp_password = created_payload["password"]
    assert temp_password != "final-pass"  # the admin-set password must never be the real one

    # self-login uses that exact temp password, against the same appliance
    # on_error=None here since this test didn't pass on_step - provision_monitor_user
    # wires the self-login session's on_error to whatever on_step it was given.
    mock_luna_session.assert_called_once_with(username="mon", password=temp_password, on_error=None, **SESSION_KWARGS)

    # the self-authenticated session is what sets the FINAL password, via the documented
    # changePassword action (self-scoped by the framework, unlike a raw PUT /users/{id})
    self_session.post.assert_called_once()
    self_args, self_kwargs = self_session.post.call_args
    assert self_args[0] == "/users/mon/actions/changePassword"
    change_payload = json.loads(self_kwargs["payload"])
    assert change_payload == {"currentPassword": temp_password, "password": "final-pass"}
    assert self_kwargs["expected_status_codes"] == (204,)

    assert any("created user" in s for s in steps)
    assert any("set its password" in s for s in steps)
    assert any("read back ACL" in s and "matches" in s for s in steps)


@patch("app.luna.provisioning.LunaSession")
def test_existing_role_is_kept_but_existing_user_is_deleted_and_recreated(mock_luna_session):
    admin_session = _admin_session(roles=[{"id": "mon"}], users=[{"id": "mon"}])
    self_session = _self_session_mock(mock_luna_session)

    steps = provision_monitor_user(admin_session, "mon", "final-pass", SESSION_KWARGS)

    assert not any(c.args[0] == "/roles" for c in admin_session.post.call_args_list)
    # ACL still applied even though the role already existed - regression guard for the
    # old nautobot plugin bug where a fresh role got zero permissions until run twice.
    admin_session.put.assert_any_call(
        "/roles/mon/resources",
        payload=MONITOR_ROLE_ACL,
        headers={
            "Content-Type": "application/vnd.safenetinc.lunasa+octet-stream;version=15",
            "Accept": "application/octet-stream",
        },
        expected_status_codes=(202, 204),
    )

    admin_session.delete.assert_called_once_with("/users/mon", expected_status_codes=(204,))
    assert any(c.args[0] == "/users" for c in admin_session.post.call_args_list)

    self_session.post.assert_called_once()
    assert self_session.post.call_args.args[0] == "/users/mon/actions/changePassword"
    assert json.loads(self_session.post.call_args.kwargs["payload"])["password"] == "final-pass"

    assert any("role 'mon' already exists" in s for s in steps)
    assert any("deleted existing user" in s for s in steps)


@patch("app.luna.provisioning.LunaSession")
def test_acl_mismatch_after_put_is_reported_not_silently_trusted(mock_luna_session):
    # Regression guard for exactly the "we PUT it, does that mean it's live?" question
    # this was built to answer - a role that already existed with a stale/truncated
    # ACL must be flagged, not assumed correct just because the PUT itself returned 2xx.
    stale_acl = "GET:/\nGET:/api\n"
    admin_session = _admin_session(roles=[{"id": "mon"}], users=[{"id": "mon"}], live_acl=stale_acl)
    _self_session_mock(mock_luna_session)

    steps = provision_monitor_user(admin_session, "mon", "final-pass", SESSION_KWARGS)

    mismatch_step = next(s for s in steps if "read back ACL" in s)
    assert "DOES NOT MATCH" in mismatch_step
    assert stale_acl in mismatch_step


@patch("app.luna.provisioning.LunaSession")
def test_acl_readback_failure_is_reported_but_does_not_break_provisioning(mock_luna_session):
    admin_session = _admin_session(roles=[], users=[])
    admin_session.get.side_effect = RuntimeError("500 Internal Server Error")
    _self_session_mock(mock_luna_session)

    steps = provision_monitor_user(admin_session, "mon", "final-pass", SESSION_KWARGS)

    assert any("could not read back the role's ACL" in s for s in steps)
    # The diagnostic failing must not stop the rest of provisioning from completing.
    assert any("set its password" in s for s in steps)


def test_get_role_acl_reads_the_current_resources_for_that_role():
    session = MagicMock()
    session.api_version = 15
    session.get.return_value = MagicMock(text="GET:/\n")

    acl = get_role_acl(session, "mon")

    assert acl == "GET:/\n"
    # Regression guard: omitting Content-Type here 400s with
    # FRAMEWORK_HEADER_DOES_NOT_MATCH_MEDIA_TYPE_TEMPLATE even though this is a
    # bodyless GET - confirmed against the real appliance, not just guessed.
    session.get.assert_called_once_with(
        "/roles/mon/resources",
        headers={
            "Content-Type": "application/vnd.safenetinc.lunasa+octet-stream;version=15",
            "Accept": "application/octet-stream",
        },
    )


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
