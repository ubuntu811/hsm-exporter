import logging
import os
import tomllib
from pathlib import Path

from flask import Flask

from .monitor import load_config
from .poller import init_pollers
from .routes import web

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    # threadName is free identification for which HSM a call belongs to - each
    # poller thread is named "poller-<hsm-name>" in app/poller.py.
    format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
)


def _read_version() -> str:
    """MAJOR.MINOR from pyproject.toml + BUILD_NUMBER baked into the image at build
    time (see Containerfile/build.sh) - never lets a missing/malformed pyproject.toml
    take the whole app down just for a version string."""
    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as handle:
            base_version = tomllib.load(handle)["project"]["version"]
    except Exception:  # noqa: BLE001 - a version string is never worth crashing over
        base_version = "unknown"

    build_number = os.environ.get("BUILD_NUMBER", "dev")
    return f"{base_version}.{build_number}"


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(web)

    version = _read_version()

    @app.context_processor
    def inject_version() -> dict[str, str]:
        return {"app_version": version}

    # Under `flask run --debug` the reloader re-imports this module in its watcher
    # process too; only the child that actually serves requests sets this env var.
    # gunicorn (production) never sets it and only imports once, so this is a no-op there.
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        init_pollers(load_config())

    return app
