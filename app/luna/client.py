from __future__ import annotations

from typing import Any

from .session import LunaSession


class LunaClient:
    """The handful of Luna REST calls this app actually needs."""

    def __init__(self, session: LunaSession) -> None:
        self.session = session

    def get_hsm(self) -> dict[str, Any]:
        """Each Luna Network HSM appliance manages exactly one physical HSM card,
        so /api/lunasa/hsms always returns a single entry."""
        hsms = self.session.get_json("/api/lunasa/hsms")["hsms"]
        return hsms[0] if hsms else {}

    def list_partitions(self, hsm_id: str) -> list[dict[str, Any]]:
        return self.session.get_json(f"/api/lunasa/hsms/{hsm_id}/partitions")["partitions"]

    def list_roles(self, hsm_id: str, partition_id: str) -> list[dict[str, Any]]:
        """Returns role stubs only (id/name/url per Thales's docs) - fetch each one's
        own `url` via get_role() for activated/initialized status."""
        return self.session.get_json(f"/api/lunasa/hsms/{hsm_id}/partitions/{partition_id}/roles")["roles"]

    def get_role(self, role_path: str) -> dict[str, Any]:
        return self.session.get_json(role_path)
