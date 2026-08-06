from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from app import state
from app.monitor import (
    FatalHsmError,
    poll_clients_once,
    poll_once,
    role_expectations,
)

logger = logging.getLogger(__name__)

ROLES_POLL_INTERVAL_SECONDS = 60
# Client registration changes rarely (nothing like login state) and the fan-out is
# uncapped (one call per registered client, plus one per link) - a long interval keeps
# it from being the thing that hammers the appliance.
CLIENTS_POLL_INTERVAL_SECONDS = 600


class _IntervalPoller:
    """Shared "run one cycle on a timer, in its own thread" mechanics: start/stop/
    check_now, self-stops on FatalHsmError. Subclasses implement _poll_cycle() (call
    state.update() with whatever fields this poller owns) and _on_fatal(exc)."""

    thread_prefix = "poller"

    def __init__(self, name: str, interval: float) -> None:
        self.name = name
        self.interval = interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_alive():
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(target=self._run, name=f"{self.thread_prefix}-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()  # wake it immediately instead of waiting out the interval

    def check_now(self) -> None:
        """Poll immediately. Resumes the poller first if it had fatally stopped."""
        if not self.is_alive():
            self.start()
            return
        self._wake.set()

    def _on_started(self) -> None:
        pass

    def _poll_cycle(self) -> None:
        raise NotImplementedError

    def _on_fatal(self, exc: FatalHsmError) -> None:
        raise NotImplementedError

    def _on_stopped(self) -> None:
        pass

    def _run(self) -> None:
        self._on_started()

        while not self._stop.is_set():
            try:
                self._poll_cycle()
            except FatalHsmError as exc:
                logger.warning("%s: fatal, stopping %s: %s", self.name, self.thread_prefix, exc)
                self._on_fatal(exc)
                return

            self._wake.wait(self.interval)
            self._wake.clear()

        self._on_stopped()


class RolesPoller(_IntervalPoller):
    """CU/CO login-state signal - the one you actually want checked every minute."""

    thread_prefix = "roles-poller"

    def __init__(
        self,
        entry: dict[str, Any],
        global_config: dict[str, Any],
        role_expectations: dict[str, dict[str, Any]],
        username: str,
        password: str,
        interval: float = ROLES_POLL_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(entry["name"], interval)
        self.entry = entry
        self.global_config = global_config
        self.role_expectations = role_expectations
        self.username = username
        self.password = password

    def _on_started(self) -> None:
        state.update(self.name, {"thread_status": "running"})

    def _poll_cycle(self) -> None:
        result = poll_once(self.entry, self.global_config, self.role_expectations, self.username, self.password)
        result["role_problems"] = result.pop("problems")
        result["thread_status"] = "running"
        result["last_checked"] = time.time()
        state.update(self.name, result)

    def _on_fatal(self, exc: FatalHsmError) -> None:
        state.update(
            self.name,
            {
                "role_problems": [
                    {
                        "severity": "fatal",
                        "kind": "connection_failed",
                        "message": "could not connect to the hsm - monitoring thread stopped",
                        "hsm": self.name,
                    }
                ],
                "thread_status": "stopped",
                "last_checked": time.time(),
            },
        )

    def _on_stopped(self) -> None:
        state.update(self.name, {"thread_status": "stopped"})


class ClientsPoller(_IntervalPoller):
    """NTLS client-to-partition mapping - much slower-changing, much larger fan-out,
    isolated in its own thread so it never delays the roles/login-state signal."""

    thread_prefix = "clients-poller"

    def __init__(
        self,
        entry: dict[str, Any],
        global_config: dict[str, Any],
        username: str,
        password: str,
        interval: float = CLIENTS_POLL_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(entry["name"], interval)
        self.entry = entry
        self.global_config = global_config
        self.username = username
        self.password = password

    def _on_started(self) -> None:
        state.update(self.name, {"clients_thread_status": "running"})

    def _poll_cycle(self) -> None:
        partition_clients = poll_clients_once(self.entry, self.global_config, self.username, self.password)
        state.update(
            self.name,
            {
                "partition_clients": partition_clients,
                "client_problems": [],
                "clients_thread_status": "running",
                "clients_last_checked": time.time(),
            },
        )

    def _on_fatal(self, exc: FatalHsmError) -> None:
        state.update(
            self.name,
            {
                "client_problems": [
                    {
                        "severity": "fatal",
                        "kind": "connection_failed",
                        "message": "could not connect to the hsm - client monitoring thread stopped",
                        "hsm": self.name,
                    }
                ],
                "clients_thread_status": "stopped",
                "clients_last_checked": time.time(),
            },
        )

    def _on_stopped(self) -> None:
        state.update(self.name, {"clients_thread_status": "stopped"})


class HsmMonitor:
    """Unified control surface for one HSM's two independent poller threads. The UI
    treats "monitoring this HSM" as one thing to start/stop/check-now; the roles and
    clients pollers stay isolated from each other underneath regardless."""

    def __init__(self, roles: RolesPoller, clients: ClientsPoller) -> None:
        self.roles = roles
        self.clients = clients

    def start(self) -> None:
        self.roles.start()
        self.clients.start()

    def stop(self) -> None:
        self.roles.stop()
        self.clients.stop()

    def check_now(self) -> None:
        self.roles.check_now()
        self.clients.check_now()


POLLERS: dict[str, HsmMonitor] = {}


def init_pollers(config: dict[str, Any]) -> None:
    """Build one HsmMonitor (a roles poller + a clients poller) per configured HSM and
    start both. Safe to call once at app startup; calling again is a no-op for HSMs
    that already have one."""
    username = os.environ["LUNA_USERNAME"]
    password = os.environ["LUNA_PASSWORD"]
    global_config = config.get("global", {})
    expectations = role_expectations(config)

    for entry in config.get("hsms", []):
        name = entry["name"]
        state.seed(name)
        if name in POLLERS:
            continue
        roles = RolesPoller(entry, global_config, expectations, username, password)
        clients = ClientsPoller(entry, global_config, username, password)
        monitor = HsmMonitor(roles, clients)
        POLLERS[name] = monitor
        monitor.start()
