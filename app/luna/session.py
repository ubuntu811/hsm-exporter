from __future__ import annotations

import logging
from typing import Any

import requests
import urllib3
from requests.auth import HTTPBasicAuth

from .exceptions import LunaApiError

# verify=False against these appliances' self-signed certs is a deliberate, permanent
# choice here, not an oversight - the warning is just noise on every single request.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class LunaSession:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        verify: bool | str = False,
        timeout: float = 10.0,
        api_version: int = 15,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.verify = verify
        self.timeout = timeout
        self.api_version = api_version

        self.headers = {
            "Content-Type": (
                "application/vnd.safenetinc.lunasa+json;"
                f"version={api_version}"
            )
        }

        self.session_cookie: requests.cookies.RequestsCookieJar | None = None

        self.get_session(
            username=username,
            password=password,
        )

    def get_session(
        self,
        username: str,
        password: str,
    ) -> None:
        response = self.request(
            "POST",
            "/auth/session",
            auth=HTTPBasicAuth(username, password),
            expected_status_codes=(204,),
        )
        self.session_cookie = response.cookies.copy()

    def logout_session(self) -> None:
        if self.session_cookie is None:
            return

        try:
            self.request("DELETE", "/auth/session", expected_status_codes=(204,))
        finally:
            self.session_cookie = None

    def request(
        self,
        method: str,
        path: str,
        *,
        expected_status_codes: tuple[int, ...],
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        url = (
            path
            if path.startswith(("http://", "https://"))
            else f"{self.base_url}/{path.lstrip('/')}"
        )

        request_headers = headers or self.headers

        kwargs.setdefault("cookies", self.session_cookie)
        kwargs.setdefault("verify", self.verify)
        kwargs.setdefault("timeout", self.timeout)

        logger.info("%s %s", method.upper(), url)

        try:
            response = requests.request(
                method=method,
                url=url,
                headers=request_headers,
                **kwargs,
            )
        except requests.RequestException as exc:
            logger.warning("%s %s failed: %s", method.upper(), url, exc)
            raise LunaApiError(
                f"{method.upper()} {url}: {exc}"
            ) from exc

        logger.info("%s %s -> %s", method.upper(), url, response.status_code)

        if response.status_code not in expected_status_codes:
            raise LunaApiError(
                f"{method.upper()} {url}: expected HTTP "
                f"{expected_status_codes}, got {response.status_code}: "
                f"{response.content!r}",
                status_code=response.status_code,
                response_body=response.text,
            )

        return response

    def get_json(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> Any:
        response = self.request(
            "GET",
            path,
            headers=headers,
            expected_status_codes=(200,),
        )

        try:
            return response.json()
        except requests.JSONDecodeError as exc:
            raise LunaApiError(
                f"GET {path}: invalid JSON response: "
                f"{response.text[:2000]}"
            ) from exc

    def get(
        self,
        path: str,
        *,
        expected_status_codes: tuple[int, ...] = (200,),
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        return self.request(
            "GET",
            path,
            headers=headers,
            expected_status_codes=expected_status_codes,
        )

    def post(
        self,
        path: str,
        *,
        payload: str | bytes | None = None,
        expected_status_codes: tuple[int, ...] = (204,),
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        return self.request(
            "POST",
            path,
            data=payload,
            headers=headers,
            expected_status_codes=expected_status_codes,
        )

    def put(
        self,
        path: str,
        *,
        payload: str | bytes | None = None,
        expected_status_codes: tuple[int, ...] = (202, 204),
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        return self.request(
            "PUT",
            path,
            data=payload,
            headers=headers,
            expected_status_codes=expected_status_codes,
        )

    def delete(
        self,
        path: str,
        *,
        expected_status_codes: tuple[int, ...] = (204,),
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        return self.request(
            "DELETE",
            path,
            headers=headers,
            expected_status_codes=expected_status_codes,
        )

    def close(self) -> None:
        self.logout_session()

    def __enter__(self) -> "LunaSession":
        return self

    def __exit__(self, *_args: Any) -> None:
        # Best-effort cleanup: never let a logout failure mask/replace an exception
        # that's already propagating out of the `with` block.
        try:
            self.close()
        except LunaApiError:
            pass
