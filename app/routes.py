from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from flask import Blueprint, Response, abort, jsonify, redirect, render_template, request, url_for

from app import state
from app.luna.exceptions import LunaApiError
from app.luna.provisioning import ADMIN_USERNAME, RESERVED_USERNAMES, provision_monitor_user
from app.luna.session import LunaSession
from app.monitor import find_hsm_entry, load_config, suggest_partition_config
from app.monitor import session_kwargs as luna_session_kwargs
from app.poller import POLLERS

web = Blueprint(
    "web",
    __name__,
    template_folder="templates",
)


def _back_to(name: str) -> Response:
    return redirect(request.referrer or url_for("web.hsm_detail", name=name))


def _with_display_timestamp(hsm: dict[str, Any]) -> dict[str, Any]:
    ts = hsm.get("last_checked")
    hsm["last_checked_display"] = (
        datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "never"
    )
    return hsm


@web.get("/")
def index():
    hsms = sorted(state.get_all(), key=lambda hsm: hsm["name"])
    return render_template("index.html", hsms=[_with_display_timestamp(hsm) for hsm in hsms])


@web.get("/hsms")
def hsms_redirect():
    # /hsms/<name> exists, so /hsms itself is a natural guess for "list them all".
    return redirect(url_for("web.index"))


@web.get("/hsms/<name>")
def hsm_detail(name: str):
    hsm = state.get(name)
    if hsm is None:
        abort(404)
    return render_template(
        "hsm_detail.html",
        hsm=_with_display_timestamp(hsm),
        monitor_username=os.environ.get("LUNA_USERNAME", ""),
    )


@web.post("/hsms/<name>/start")
def start_poller(name: str):
    poller = POLLERS.get(name)
    if poller is None:
        abort(404)
    poller.start()
    return _back_to(name)


@web.post("/hsms/<name>/stop")
def stop_poller(name: str):
    poller = POLLERS.get(name)
    if poller is None:
        abort(404)
    poller.stop()
    return _back_to(name)


@web.post("/hsms/<name>/check-now")
def check_now(name: str):
    poller = POLLERS.get(name)
    if poller is None:
        abort(404)
    poller.check_now()
    return _back_to(name)


@web.get("/hsms/<name>/suggested-partitions.yaml")
def suggested_partitions(name: str):
    hsm = state.get(name)
    if hsm is None:
        abort(404)
    body = suggest_partition_config(load_config(), hsm)
    return Response(body, mimetype="text/plain")


@web.get("/api/v1/hsms")
def api_hsms():
    return jsonify({"hsms": state.get_all()})


@web.post("/hsms/<name>/setup")
def setup_hsm(name: str):
    admin_password = (request.get_json(silent=True) or {}).get("admin_password")
    if not admin_password:
        return jsonify({"ok": False, "error": "admin_password is required"}), 400

    config = load_config()
    entry = find_hsm_entry(config, name)
    if entry is None:
        return jsonify({"ok": False, "error": f"no configured HSM named {name!r}"}), 404

    try:
        monitor_username = os.environ["LUNA_USERNAME"]
        monitor_password = os.environ["LUNA_PASSWORD"]
    except KeyError as exc:
        return jsonify({"ok": False, "error": f"{exc.args[0]} is not set in the environment"}), 500

    if monitor_username in RESERVED_USERNAMES:
        return jsonify(
            {
                "ok": False,
                "error": (
                    f"LUNA_USERNAME is '{monitor_username}', one of the appliance's built-in "
                    f"roles ({', '.join(sorted(RESERVED_USERNAMES))}). Refusing to touch it."
                ),
            }
        ), 400

    global_config = config.get("global", {})
    kwargs = luna_session_kwargs(entry, global_config)

    try:
        with LunaSession(username=ADMIN_USERNAME, password=admin_password, **kwargs) as session:
            steps = provision_monitor_user(session, monitor_username, monitor_password, kwargs)
    except LunaApiError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    # The monitor account's credentials just (potentially) changed - refresh promptly
    # instead of waiting out the rest of the poll interval with stale/fatal state.
    poller = POLLERS.get(name)
    if poller is not None:
        poller.check_now()

    return jsonify({"ok": True, "steps": steps})


@web.get("/health")
def health():
    return jsonify({"status": "ok"})
