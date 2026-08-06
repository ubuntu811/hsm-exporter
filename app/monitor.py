from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import yaml

from app.luna.client import LunaClient
from app.luna.provisioning import RESERVED_USERNAMES
from app.luna.session import LunaSession


def load_config() -> dict[str, Any]:
    config_path = Path(os.environ.get("LUNA_CONFIG", "/config/hsms.yml"))

    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    if not isinstance(config, dict):
        raise ValueError(f"{config_path} must contain a YAML mapping")

    return config


def find_hsm_entry(config: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((entry for entry in config.get("hsms", []) if entry["name"] == name), None)


def session_kwargs(entry: dict[str, Any], global_config: dict[str, Any]) -> dict[str, Any]:
    name = entry["name"]
    return {
        "base_url": entry.get("url", f"https://{name}:8443"),
        "verify": entry.get("verify", global_config.get("verify", False)),
        "timeout": entry.get("timeout", global_config.get("timeout", 10)),
        "api_version": entry.get("api_version", global_config.get("api_version", 15)),
    }


def suggest_partition_config(config: dict[str, Any], hsm: dict[str, Any]) -> str:
    """Best-effort `partitions:` config snippet: matches each partition's currently
    observed CU/CO activation state against the templates already defined in
    `partition_templates:`. Never invents a new template - a partition that doesn't
    match any existing one is listed commented-out with its actual state instead,
    for a human to sort out."""
    templates = config.get("partition_templates", [])

    lines = ["partitions:"]
    for partition in hsm.get("partitions", []):
        label = partition.get("label") or partition.get("id")
        status = partition.get("role_status", {})
        cu_actual = status.get("cu", {}).get("actual")
        co_actual = status.get("co", {}).get("actual")

        match = None
        if cu_actual is not None and co_actual is not None:
            for template in templates:
                if (
                    (template.get("cu") == "activated") == cu_actual
                    and (template.get("co") == "activated") == co_actual
                ):
                    match = template.get("name")
                    break

        if match:
            lines.append(f"  - name: {label}")
            lines.append(f"    template: {match}")
        else:
            lines.append(f"  # no matching template for {label} (cu={cu_actual}, co={co_actual})")

    return "\n".join(lines) + "\n"


def role_expectations(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{partition_name_lower: template} from top-level `partition_templates:` +
    `partitions:` (name -> template) config sections."""
    templates = {str(t["name"]).lower(): t for t in config.get("partition_templates", [])}

    expectations: dict[str, dict[str, Any]] = {}
    for entry in config.get("partitions", []):
        template = templates.get(str(entry.get("template", "")).lower())
        if template is not None:
            expectations[str(entry["name"]).lower()] = template

    return expectations


# Matches the nautobot plugin's fetch_hsm_data.py / diffsync/hsmdata.py, which ran
# successfully against this same appliance under REST API v12: role['id'] is literally
# "cu"/"co"/"so", and the role detail object (at role['url']) has plain "activated" and
# "initialized" booleans. No other field-name variants were ever used there.
def _role_kind(role: dict[str, Any]) -> str | None:
    role_id = str(role.get("id", "")).lower()
    return role_id if role_id in ("cu", "co") else None


def _is_activated(role: dict[str, Any]) -> bool | None:
    if "activated" in role:
        return bool(role["activated"])
    return None


def _partition_role_status(roles: list[dict[str, Any]], template: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    # Keys are "actual"/"expected", not "is"/"should" - Jinja reserves `is` for its own
    # test-expression syntax, so `status.is` would be a template syntax error.
    status: dict[str, dict[str, Any]] = {
        "cu": {"actual": None, "expected": None},
        "co": {"actual": None, "expected": None},
    }

    for role in roles:
        kind = _role_kind(role)
        if kind is not None:
            status[kind]["actual"] = _is_activated(role)

    if template:
        for kind in ("cu", "co"):
            expected = template.get(kind)
            if expected is not None:
                status[kind]["expected"] = expected == "activated"

    return status


def _partition_status(client: LunaClient, hsm_id: str, partition: dict[str, Any], role_expectations: dict[str, dict[str, Any]]) -> None:
    """Mutates `partition` in place, isolating its own failure from the rest of the HSM."""
    partition["error"] = None
    partition["roles"] = []
    partition["role_status"] = _partition_role_status([], None)

    partition_id = partition.get("id")
    if not partition_id:
        return

    try:
        role_stubs = client.list_roles(hsm_id, partition_id)
        roles = [client.get_role(stub["url"]) for stub in role_stubs if stub.get("url")]
        partition["roles"] = roles

        label = str(partition.get("label", partition_id))
        template = role_expectations.get(label.lower())
        partition["role_status"] = _partition_role_status(roles, template)
    except Exception as exc:  # noqa: BLE001 - one bad partition must not blank the whole HSM
        partition["error"] = str(exc)


class FatalHsmError(Exception):
    """The HSM itself is unreachable/unusable this cycle (auth or connection failure) -
    as opposed to a single partition being broken. Callers should stop polling on this,
    not just log it and retry next cycle - retrying a struggling appliance's web server
    every minute forever makes the underlying problem worse, not better."""


def _partition_problems(hsm_name: str, partition: dict[str, Any]) -> list[dict[str, Any]]:
    label = str(partition.get("label", partition.get("id")))
    problems: list[dict[str, Any]] = []

    if partition["error"]:
        problems.append(
            {
                "severity": "error",
                "kind": "partition_fetch_error",
                "message": f"partition {label}: {partition['error']}",
                "hsm": hsm_name,
                "partition": label,
            }
        )

    for role in ("cu", "co"):
        status = partition["role_status"][role]
        if status["expected"] is None or status["actual"] is None:
            continue
        if status["actual"] != status["expected"]:
            expected_word = "activated" if status["expected"] else "deactivated"
            problems.append(
                {
                    "severity": "error",
                    "kind": "role_mismatch",
                    "message": f"partition {label} role {role} should be {expected_word}",
                    "hsm": hsm_name,
                    "partition": label,
                    "role": role,
                }
            )

    return problems


def poll_once(
    entry: dict[str, Any],
    global_config: dict[str, Any],
    role_expectations: dict[str, dict[str, Any]],
    username: str,
    password: str,
    on_event: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """One poll of one HSM. Raises FatalHsmError if the appliance itself couldn't be
    reached/authenticated to; per-partition problems are returned, not raised."""
    name = entry["name"]

    def log(message: str) -> None:
        if on_event is not None:
            on_event(message)

    if username in RESERVED_USERNAMES:
        # Never even attempt this login - same "don't touch the built-in accounts"
        # rule as provisioning, but here to catch e.g. a misconfigured LUNA_USERNAME
        # before it starts making real login attempts as admin/operator/monitor/audit.
        raise FatalHsmError(
            f"LUNA_USERNAME is '{username}', one of the appliance's built-in roles "
            f"({', '.join(sorted(RESERVED_USERNAMES))}) - refusing to use it for monitoring"
        )

    try:
        with LunaSession(
            username=username,
            password=password,
            on_error=on_event,
            **session_kwargs(entry, global_config),
        ) as session:
            client = LunaClient(session)
            hsm = client.get_hsm()
            hsm_id = hsm.get("id")

            partitions = client.list_partitions(hsm_id) if hsm_id else []
            log(f"found {len(partitions)} partition(s)")
            for partition in partitions:
                _partition_status(client, hsm_id, partition, role_expectations)
    except Exception as exc:  # noqa: BLE001 - re-raised as a distinguished fatal type
        raise FatalHsmError(str(exc)) from exc

    problems: list[dict[str, Any]] = []
    for partition in partitions:
        problems.extend(_partition_problems(name, partition))

    log(f"roles check complete: {len(problems)} problem(s) found")

    return {
        "name": name,
        # On this appliance "id" IS the serial - there's no separate serial field.
        "id": hsm_id,
        "raw": hsm,
        "partitions": partitions,
        "problems": problems,
    }


def poll_all(config: dict[str, Any]) -> list[dict[str, Any]]:
    """One-shot poll of every configured HSM, for CLI use (probe.py) - not used by the
    web app, which polls continuously via app.poller instead."""
    username = os.environ["LUNA_USERNAME"]
    password = os.environ["LUNA_PASSWORD"]
    global_config = config.get("global", {})
    expectations = role_expectations(config)

    results = []
    for entry in config.get("hsms", []):
        name = entry["name"]
        try:
            result = poll_once(entry, global_config, expectations, username, password)
        except FatalHsmError as exc:
            result = {
                "name": name,
                "id": None,
                "raw": None,
                "partitions": [],
                "problems": [
                    {
                        "severity": "fatal",
                        "kind": "connection_failed",
                        "message": f"could not connect to the hsm: {exc}",
                        "hsm": name,
                    }
                ],
            }
        results.append(result)

    return results


def _partition_id_from_url(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def poll_clients_once(
    entry: dict[str, Any],
    global_config: dict[str, Any],
    username: str,
    password: str,
    on_event: Callable[[str], None] | None = None,
) -> dict[str, list[str]]:
    """One poll of one HSM's NTLS client registrations, returning {partition_id:
    [client_names]}. Raises FatalHsmError on connection/auth failure, same contract
    as poll_once(). One bad client or link is skipped, not fatal - mirrors
    _partition_status()'s "one bad thing doesn't blank the whole result" rule. Every
    skip is reported via `on_event` though - a silent empty result (e.g. from the ACL
    not granting the NTLS endpoints yet) is indistinguishable from "no clients are
    actually registered" otherwise, which is a real trap: it *looks* successful.

    Deliberately does NOT resolve the partition detail (name/label) the way the old
    nautobot plugin did - it already has the full partition list (with ids) from
    poll_once() in the same app, so it only needs the partition *id* out of each
    link, matched against that list at render time. Saves a network call per link
    and avoids depending on an unverified field name on that detail object."""

    def log(message: str) -> None:
        if on_event is not None:
            on_event(message)

    if username in RESERVED_USERNAMES:
        raise FatalHsmError(
            f"LUNA_USERNAME is '{username}', one of the appliance's built-in roles "
            f"({', '.join(sorted(RESERVED_USERNAMES))}) - refusing to use it for monitoring"
        )

    try:
        with LunaSession(
            username=username,
            password=password,
            on_error=on_event,
            **session_kwargs(entry, global_config),
        ) as session:
            client = LunaClient(session)
            clients = client.list_ntls_clients()
            log(f"found {len(clients)} registered client(s)")

            partition_clients: dict[str, list[str]] = {}
            resolved_count = 0
            client_failures = 0
            link_failures = 0

            for stub in clients:
                client_id = stub.get("clientID")
                client_url = stub.get("url")
                if not client_id or not client_url:
                    continue

                # Failures here are already reported via on_error (the session logs
                # every failing call with its exact URL/status/body) - just tally them
                # for the summary line below, no need to also restate each one.
                try:
                    links = client.list_client_links(client_url)
                except Exception:  # noqa: BLE001 - one bad client shouldn't blank the rest
                    client_failures += 1
                    continue

                client_link_failures = 0
                for link_stub in links:
                    link_url = link_stub.get("url")
                    if not link_url:
                        continue
                    try:
                        link = client.get_link(link_url)
                    except Exception:  # noqa: BLE001 - one bad link shouldn't blank the rest
                        link_failures += 1
                        client_link_failures += 1
                        continue
                    if link.get("type") != "hsm/partition" or not link.get("url"):
                        continue

                    partition_id = _partition_id_from_url(link["url"])
                    partition_clients.setdefault(partition_id, []).append(client_id)
                    resolved_count += 1

                if client_link_failures:
                    # The individual 404/401/etc are already visible via on_error, but
                    # not what the LIST call itself said existed - if a listed link
                    # then 404s on its own url, the raw list stubs are what's needed
                    # to tell "list is stale/wrong" apart from "we mis-built the url".
                    log(f"client '{client_id}': raw link list was {links!r}")
    except Exception as exc:  # noqa: BLE001 - re-raised as a distinguished fatal type
        raise FatalHsmError(str(exc)) from exc

    summary = f"clients check complete: {resolved_count} partition link(s) resolved across {len(partition_clients)} partition(s)"
    if client_failures or link_failures:
        summary += f" - {client_failures} client(s) and {link_failures} link(s) could not be checked"
    log(summary)

    return partition_clients
