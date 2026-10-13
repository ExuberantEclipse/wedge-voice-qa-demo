"""Small authenticated adapter for the authorized Wedge test portal."""

from __future__ import annotations

import json
import time
from http.cookiejar import CookieJar
from typing import Any, Mapping, Optional
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener


class PortalError(RuntimeError):
    pass


class WedgePortalClient:
    def __init__(self, base_url: str, timeout_seconds: int = 20):
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def _request(
        self, method: str, path: str, payload: Optional[Mapping[str, Any]] = None
    ) -> Mapping[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self._base_url + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(detail).get("error", detail)
            except json.JSONDecodeError:
                message = detail
            raise PortalError(f"Portal {method} {path} failed: {error.code} {message}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise PortalError(f"Portal {method} {path} failed: {error}") from error
        if not isinstance(result, Mapping):
            raise PortalError(f"Portal {method} {path} returned an invalid response")
        return result

    def sign_in(self, code: str, name: str, email: str) -> Mapping[str, Any]:
        result = self._request(
            "POST", "/api/session", {"code": code, "name": name, "email": email}
        )
        tester = result.get("tester")
        if not isinstance(tester, Mapping):
            raise PortalError("Portal sign-in did not return a tester")
        return tester

    def personas(self) -> list[Mapping[str, Any]]:
        result = self._request("GET", "/api/personas")
        personas = result.get("personas")
        if not isinstance(personas, list):
            raise PortalError("Portal did not return a personas array")
        return [item for item in personas if isinstance(item, Mapping)]

    def start_call(
        self,
        *,
        persona_id: str,
        language: str,
        phone: str,
        member_name: str = "",
        transfer_phone: str = "",
    ) -> str:
        result = self._request(
            "POST",
            "/api/calls",
            {
                "personaId": persona_id,
                "language": language,
                "memberName": member_name,
                "phone": phone,
                "transferPhone": transfer_phone,
            },
        )
        call_id = str(result.get("callId") or "")
        if not call_id:
            raise PortalError("Portal call start did not return a callId")
        return call_id

    def call(self, call_id: str) -> Mapping[str, Any]:
        result = self._request("GET", f"/api/calls/{call_id}")
        call = result.get("call")
        if not isinstance(call, Mapping):
            raise PortalError(f"Portal did not return call {call_id}")
        return call

    def wait_for_end(
        self, call_id: str, timeout_seconds: int = 600, poll_seconds: float = 2.0
    ) -> Mapping[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            call = self.call(call_id)
            if call.get("status") == "ended":
                return call
            time.sleep(max(0.5, poll_seconds))
        raise PortalError(f"Portal call {call_id} did not end within {timeout_seconds}s")
