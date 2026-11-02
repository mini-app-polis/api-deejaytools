"""The rate limit's caller comes from a credential that verifies, verified
once per request (middleware.py, auth.verified_credential)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from identity.clerk import ClerkVerifier

from api_deejaytools.middleware import RateLimitMiddleware

from .conftest import Clerk, bearer


def _limited_app() -> FastAPI:
    app = FastAPI()

    @app.get("/v1/ping")
    async def ping() -> dict:
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware, limit=2, window_seconds=60)
    return app


async def test_a_forged_or_expired_token_gets_the_address_window(
    clerk: Clerk,
) -> None:
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    venue = {"x-forwarded-for": "198.51.100.7"}
    transport = httpx.ASGITransport(app=_limited_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        forged = [
            await c.get(
                "/v1/ping",
                headers={**venue, **bearer(clerk.token(f"user_f{n}", key=other_key))},
            )
            for n in range(3)
        ]
        expired = await c.get(
            "/v1/ping",
            headers={**venue, **bearer(clerk.token("user_x", expires_in=-60))},
        )
        genuine = [
            await c.get("/v1/ping", headers={**venue, **bearer(clerk.token("user_g"))})
            for _ in range(3)
        ]
    assert [r.status_code for r in forged] == [200, 200, 429]
    assert expired.status_code == 429
    assert [r.status_code for r in genuine] == [200, 200, 429]


@pytest.mark.parametrize("path", ["/v1/auth/me", "/v1/sessions"])
async def test_a_request_verifies_its_credential_once(
    client: httpx.AsyncClient,
    person: Any,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    ada = await person()
    calls: list[str] = []
    verify = ClerkVerifier.verify

    async def counting(self: ClerkVerifier, credential: str) -> Any:
        calls.append(credential)
        return await verify(self, credential)

    monkeypatch.setattr(ClerkVerifier, "verify", counting)
    res = await client.get(path, headers=ada.headers)
    assert res.status_code == 200, res.text
    assert len(calls) == 1
