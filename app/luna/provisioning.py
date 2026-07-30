from __future__ import annotations

import json
import secrets
from typing import Any

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
    "GET:/api/lunasa/ntls",
    "POST:/auth/session",
    "DELETE:/auth/session",
    # Framework-enforced self-scoped: a non-admin caller can only target their own userid
    # here regardless of the {userid} wildcard, per the REST API reference for this action.
    "POST:/users/{userid}/actions/changePassword",
]
MONITOR_ROLE_ACL = "\n".join(_MONITOR_ACL_LINES)


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
) -> list[str]:
    """Using an already-authenticated admin session, ensure the role+ACL exist, then
    (re)create the monitor user from scratch. The appliance flags any password an admin
    sets directly as "must change on first use" and blocks all other API access until
    the account changes its own password - so this creates the user with a throwaway
    temp password, logs in AS that user, and has it self-service change its password to
    `final_password`. `session_kwargs` are the base_url/verify/timeout/api_version
    needed to open that second session against this same appliance."""
    if username in RESERVED_USERNAMES:
        raise ValueError(
            f"refusing to provision '{username}' - that's one of the appliance's built-in "
            f"roles ({', '.join(sorted(RESERVED_USERNAMES))}), not a name for a new REST API user"
        )

    steps: list[str] = []

    roles = admin_session.get_json("/roles").get("roles", [])
    role_exists = any(role.get("id") == username for role in roles)

    if role_exists:
        steps.append(f"role '{username}' already exists")
    else:
        admin_session.post(
            "/roles",
            payload=json.dumps({"roleId": username, "fullName": "HSM Monitor Readonly Role"}),
            expected_status_codes=(204,),
        )
        steps.append(f"created role '{username}'")

    admin_session.put(
        f"/roles/{username}/resources",
        payload=MONITOR_ROLE_ACL,
        headers={
            "Content-Type": f"application/vnd.safenetinc.lunasa+octet-stream;version={admin_session.api_version}",
            "Accept": "application/octet-stream",
        },
        expected_status_codes=(202, 204),
    )
    steps.append(f"applied ACL to role '{username}'")

    users = admin_session.get_json("/users").get("users", [])
    user_exists = any(user.get("id") == username for user in users)

    if user_exists:
        admin_session.delete(f"/users/{username}", expected_status_codes=(204,))
        steps.append(f"deleted existing user '{username}'")

    temp_password = secrets.token_urlsafe(24)
    admin_session.post("/users", payload=_user_payload(username, temp_password), expected_status_codes=(204,))
    steps.append(f"created user '{username}' with a temporary password")

    with LunaSession(username=username, password=temp_password, **session_kwargs) as self_session:
        self_session.post(
            f"/users/{username}/actions/changePassword",
            payload=json.dumps({"currentPassword": temp_password, "password": final_password}),
            expected_status_codes=(204,),
        )
    steps.append(f"logged in as '{username}' and set its password to the configured LUNA_PASSWORD")

    return steps
