from __future__ import annotations

import json
import secrets
from typing import Any, Callable

from .session import LunaSession

# The appliance's own built-in superuser account. Fixed by the appliance, not per-role.
ADMIN_USERNAME = "admin"

# The appliance's other built-in roles/accounts - never let this app create, delete, or
# reset the password on any of these, no matter what LUNA_USERNAME is set to.
RESERVED_USERNAMES = frozenset({"admin", "operator", "monitor", "audit"})

# Read-only surface for the monitor account. Carried over from the nautobot plugin that
# provisioned this same role/ACL under REST API v12 - the ACL grammar and resource paths
# are unaffected by the version bump, only the request Content-Type is.
_MONITOR_ACL_LINES = [
    "GET:/",
    "GET:/api",
    "GET:/api/lunasa/hsms",
    "GET:/api/lunasa/hsms/{hsmid}",
    "GET:/api/lunasa/hsms/{hsmid}/partitions",
    "GET:/api/lunasa/hsms/{hsmid}/partitions/{partitionid}",
    "GET:/api/lunasa/hsms/{hsmid}/partitions/{partitionid}/roles",
    # The list above only grants the stub list; each stub's own `url` (e.g. .../roles/co)
    # needs this separately or every get_role() follow-up 401s with FRAMEWORK_NO_ACL_RULE.
    "GET:/api/lunasa/hsms/{hsmid}/partitions/{partitionid}/roles/{roleid}",
    "GET:/api/lunasa/hsms/{hsmid}/partitions/{partitionid}/objects",
    "GET:/api/lunasa/hsms/{hsmid}/firmware",
    "GET:/api/lunasa/hsms/{hsmid}/capabilities",
    "GET:/api/lunasa/hsms/{hsmid}/licenses",
    "GET:/api/lunasa/hsms/{hsmid}/metrics",
    "GET:/api/lunasa/hsms/{hsmid}/utilization",
    "GET:/api/lunasa/sensors",
    "GET:/api/lunasa/cpu",
    "GET:/api/lunasa/disk",
    "GET:/api/lunasa/network",
    # Copied verbatim from the old nautobot plugin's ACL text as a debugging test -
    # {clientid} vs {clientName} and the added intermediate resource still 401'd on
    # .../links, so this rules out casing/wording of the placeholder as the cause.
    # Includes /certificate and /partitions even though nothing here calls them, to
    # match the old text exactly rather than guess at which lines actually mattered.
    "GET:/api/lunasa/ntls",
    "GET:/api/lunasa/ntls/clients",
    "GET:/api/lunasa/ntls/clients/{clientName}",
    "GET:/api/lunasa/ntls/certificate",
    "GET:/api/lunasa/ntls/clients/{clientName}/links",
    "GET:/api/lunasa/ntls/clients/{clientName}/links/{linkid}",
    "GET:/api/lunasa/ntls/clients/{clientName}/partitions",
    "POST:/auth/session",
    "DELETE:/auth/session",
    # Framework-enforced self-scoped: a non-admin caller can only target their own userid
    # here regardless of the {userid} wildcard, per the REST API reference for this action.
    "POST:/users/{userid}/actions/changePassword",
]
MONITOR_ROLE_ACL = "\n".join(_MONITOR_ACL_LINES)


def get_role_acl(admin_session: LunaSession, username: str) -> str:
    """Read back the ACL actually stored on the appliance for this role, rather than
    trusting that a successful PUT means it took - the mismatch between "we sent it"
    and "it's actually active" is exactly what's in question when the ACL keeps
    getting rewritten but the resulting 401s never change.

    Needs the same Content-Type as the PUT to this same endpoint, even though this is
    a bodyless GET - omitting it (passing only Accept) 400s with
    FRAMEWORK_HEADER_DOES_NOT_MATCH_MEDIA_TYPE_TEMPLATE. Passing a `headers` dict to
    LunaSession.request() replaces the session's default headers entirely rather than
    merging, so both need to be given explicitly here."""
    response = admin_session.get(
        f"/roles/{username}/resources",
        headers={
            "Content-Type": f"application/vnd.safenetinc.lunasa+octet-stream;version={admin_session.api_version}",
            "Accept": "application/octet-stream",
        },
    )
    return response.text


def _user_payload(username: str, password: str) -> str:
    return json.dumps(
        {
            "userId": username,
            "fullName": "HSM Monitor Service Account",
            "role": username,
            "password": password,
        }
    )


def provision_monitor_user(
    admin_session: LunaSession,
    username: str,
    final_password: str,
    session_kwargs: dict[str, Any],
    on_step: Callable[[str], None] | None = None,
) -> list[str]:
    """Using an already-authenticated admin session, ensure the role+ACL exist, then
    (re)create the monitor user from scratch. The appliance flags any password an admin
    sets directly as "must change on first use" and blocks all other API access until
    the account changes its own password - so this creates the user with a throwaway
    temp password, logs in AS that user, and has it self-service change its password to
    `final_password`. `session_kwargs` are the base_url/verify/timeout/api_version
    needed to open that second session against this same appliance.

    `on_step`, if given, is called with each step's message as soon as that step
    completes - not just at the end. If a later step raises, whatever `on_step` was
    already called with is not lost the way the returned list would be."""
    if username in RESERVED_USERNAMES:
        raise ValueError(
            f"refusing to provision '{username}' - that's one of the appliance's built-in "
            f"roles ({', '.join(sorted(RESERVED_USERNAMES))}), not a name for a new REST API user"
        )

    steps: list[str] = []

    def record(message: str) -> None:
        steps.append(message)
        if on_step is not None:
            on_step(message)

    roles = admin_session.get_json("/roles").get("roles", [])
    role_exists = any(role.get("id") == username for role in roles)

    if role_exists:
        record(f"role '{username}' already exists")
    else:
        admin_session.post(
            "/roles",
            payload=json.dumps({"roleId": username, "fullName": "HSM Monitor Readonly Role"}),
            expected_status_codes=(204,),
        )
        record(f"created role '{username}'")

    admin_session.put(
        f"/roles/{username}/resources",
        payload=MONITOR_ROLE_ACL,
        headers={
            "Content-Type": f"application/vnd.safenetinc.lunasa+octet-stream;version={admin_session.api_version}",
            "Accept": "application/octet-stream",
        },
        expected_status_codes=(202, 204),
    )
    record(f"applied ACL to role '{username}'")

    try:
        live_acl = get_role_acl(admin_session, username)
    except Exception as exc:  # noqa: BLE001 - diagnostic only, must not break provisioning
        record(f"could not read back the role's ACL to verify: {exc}")
    else:
        if live_acl.strip() == MONITOR_ROLE_ACL.strip():
            record(f"read back ACL for role '{username}' - matches what was just sent")
        else:
            record(f"read back ACL for role '{username}' - DOES NOT MATCH what was sent:\n{live_acl}")

    users = admin_session.get_json("/users").get("users", [])
    user_exists = any(user.get("id") == username for user in users)

    if user_exists:
        admin_session.delete(f"/users/{username}", expected_status_codes=(204,))
        record(f"deleted existing user '{username}'")

    temp_password = secrets.token_urlsafe(24)
    admin_session.post("/users", payload=_user_payload(username, temp_password), expected_status_codes=(204,))
    record(f"created user '{username}' with a temporary password")

    with LunaSession(username=username, password=temp_password, on_error=on_step, **session_kwargs) as self_session:
        self_session.post(
            f"/users/{username}/actions/changePassword",
            payload=json.dumps({"currentPassword": temp_password, "password": final_password}),
            expected_status_codes=(204,),
        )
    record(f"logged in as '{username}' and set its password to the configured LUNA_PASSWORD")

    return steps
