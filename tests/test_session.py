from __future__ import annotations

from unittest.mock import patch

import pytest

from app.luna.exceptions import LunaApiError
from app.luna.session import LunaSession


def _mock_response(status_code: int) -> object:
    class _Response:
        def __init__(self) -> None:
            self.status_code = status_code
            self.content = b""
            self.cookies = {}

        def copy(self):
            return self

    return _Response()


@patch("app.luna.session.requests.request")
def test_content_type_defaults_to_api_version_15(mock_request):
    mock_request.return_value = _mock_response(204)

    LunaSession(base_url="https://hsm.example:8443", username="u", password="p")

    assert mock_request.call_args.kwargs["method"] == "POST"
    sent_headers = mock_request.call_args.kwargs["headers"]
    assert sent_headers["Content-Type"] == "application/vnd.safenetinc.lunasa+json;version=15"


@patch("app.luna.session.requests.request")
def test_content_type_honors_explicit_api_version(mock_request):
    mock_request.return_value = _mock_response(204)

    LunaSession(base_url="https://hsm.example:8443", username="u", password="p", api_version=12)

    sent_headers = mock_request.call_args.kwargs["headers"]
    assert sent_headers["Content-Type"] == "application/vnd.safenetinc.lunasa+json;version=12"


@patch("app.luna.session.requests.request")
def test_non_204_on_auth_raises_with_status_and_message(mock_request):
    mock_request.return_value = _mock_response(401)

    with pytest.raises(LunaApiError) as excinfo:
        LunaSession(base_url="https://hsm.example:8443", username="u", password="wrong")

    # Regression guard: LunaApiError(msg, status_code=..., response_body=...) must not
    # itself blow up with "takes no keyword arguments" and swallow the real error.
    assert excinfo.value.status_code == 401
    assert "401" in str(excinfo.value)


@patch("app.luna.session.requests.request")
def test_logout_uses_delete_not_post(mock_request):
    # Regression guard for the ACL bug: logout_session() must send DELETE, since that's
    # the method the monitor role's ACL was written against.
    mock_request.return_value = _mock_response(204)

    session = LunaSession(base_url="https://hsm.example:8443", username="u", password="p")
    mock_request.reset_mock()
    mock_request.return_value = _mock_response(204)

    session.logout_session()

    assert mock_request.call_args.kwargs["method"] == "DELETE"
    assert mock_request.call_args.kwargs["url"] == "https://hsm.example:8443/auth/session"
