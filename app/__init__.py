import logging
import os

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


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(web)

    # Under `flask run --debug` the reloader re-imports this module in its watcher
    # process too; only the child that actually serves requests sets this env var.
    # gunicorn (production) never sets it and only imports once, so this is a no-op there.
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        init_pollers(load_config())

    return app
