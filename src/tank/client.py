"""HTTP client to Headjack — wrapped so a network failure becomes a
`HeadjackUnavailable` the caller can turn into a degraded tool result, never a raw
exception that aborts the agent's turn mid-tool-call.
"""

from __future__ import annotations

from typing import Any

import httpx

from tank.config import TankConfig


class HeadjackUnavailable(Exception):
    """Headjack could not be reached in time. Every tool function catches this
    and returns a tool result describing the degradation — see spec §5's offline
    requirement. This must never propagate past a tool function."""


class HeadjackClient:
    def __init__(self, config: TankConfig) -> None:
        self._config = config
        self._http = httpx.Client(
            base_url=config.headjack_url,
            timeout=config.timeout_s,
            headers={"Authorization": f"Bearer {config.token}"},
        )

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._request("PUT", path, **kwargs)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise HeadjackUnavailable(
                f"could not connect to Headjack at {self._config.headjack_url}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise HeadjackUnavailable(
                f"Headjack did not respond within {self._config.timeout_s}s"
            ) from exc
        except httpx.TransportError as exc:
            # The rest of the family — a dropped read, a proxy refusing, a
            # malformed response at the protocol level. Catching only the two
            # named above left the others to abort the tool call, which is the
            # single thing this class exists to prevent.
            raise HeadjackUnavailable(f"request to Headjack failed: {exc}") from exc

    def close(self) -> None:
        self._http.close()
