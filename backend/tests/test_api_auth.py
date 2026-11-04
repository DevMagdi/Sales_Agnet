"""Auth, RBAC, system, public pages and metrics over real ASGI requests."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient


async def test_health_and_readiness(client: AsyncClient) -> None:
    health = await client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    ready = await client.get("/api/v1/system/health")
    assert ready.status_code in {200, 401}


async def test_login_me_and_logout(client: AsyncClient, seeded: dict[str, Any]) -> None:
    login = await client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "AdminPassw0rd!"})
    assert login.status_code == 200, login.text
    body = login.json()
    assert body["access_token"]
    assert body["refresh_token"]
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == "admin@example.com"
    refreshed = await client.post("/api/v1/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"]
    # the old refresh token is rotated (single use)
    replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert replay.status_code in {401, 400}
    logout = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": refreshed.json()["refresh_token"]}, headers=headers
    )
    assert logout.status_code == 200
    after = await client.post("/api/v1/auth/refresh", json={"refresh_token": refreshed.json()["refresh_token"]})
    assert after.status_code in {401, 400}


async def test_wrong_password_is_generic(client: AsyncClient, seeded: dict[str, Any]) -> None:
    response = await client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "wrong"})
    assert response.status_code == 401
    assert "password" in response.text.lower() or "invalid" in response.text.lower()
    missing = await client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "x"})
    assert missing.status_code == 401


async def test_protected_endpoint_requires_token(client: AsyncClient, seeded: dict[str, Any]) -> None:
    response = await client.get("/api/v1/companies")
    assert response.status_code == 401


async def test_rbac_blocks_viewer_writes(client: AsyncClient, seeded: dict[str, Any], session: Any) -> None:
    from app.services import seed

    await seed.ensure_user(
        session, email="viewer@example.com", password="ViewerPassw0rd!", role="viewer", name="Viewer"
    )
    await session.flush()
    login = await client.post(
        "/api/v1/auth/login", json={"email": "viewer@example.com", "password": "ViewerPassw0rd!"}
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    read = await client.get("/api/v1/companies", headers=headers)
    assert read.status_code == 200
    write = await client.post("/api/v1/companies", json={"name": "Nope"}, headers=headers)
    assert write.status_code == 403


async def test_system_endpoints(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    status = await client.get("/api/v1/system/status", headers=auth_headers)
    assert status.status_code == 200, status.text
    payload = status.json()
    assert "mode" in str(payload).lower()
    adapters = await client.get("/api/v1/system/adapters", headers=auth_headers)
    assert adapters.status_code == 200
    doctor = await client.get("/api/v1/system/doctor", headers=auth_headers)
    assert doctor.status_code == 200, doctor.text
    assert "checks" in doctor.json()
    kill = await client.post(
        "/api/v1/system/kill", json={"active": True, "reason": "test"}, headers=auth_headers
    )
    assert kill.status_code == 200, kill.text
    status2 = await client.get("/api/v1/system/status", headers=auth_headers)
    assert status2.json()["kill_switch"] is True
    resume = await client.post("/api/v1/system/resume", json={}, headers=auth_headers)
    assert resume.status_code == 200, resume.text
    status3 = await client.get("/api/v1/system/status", headers=auth_headers)
    assert status3.json()["kill_switch"] is False


async def test_mode_change_requires_confirmation(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    no_confirm = await client.post("/api/v1/system/mode", json={"mode": "live"}, headers=auth_headers)
    assert no_confirm.status_code in {400, 409, 422}
    confirmed = await client.post(
        "/api/v1/system/mode", json={"mode": "live", "confirm_live": True}, headers=auth_headers
    )
    # blockers exist (fake providers over HTTP test env), so it refuses with the list
    assert confirmed.status_code in {400, 403, 409, 422}, confirmed.text
    assert "blockers" in confirmed.text


async def test_budgets_endpoint(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    budgets = await client.post(
        "/api/v1/system/budgets",
        json={"llm_daily_usd": 3.5, "api_daily_usd": 2.0},
        headers=auth_headers,
    )
    assert budgets.status_code == 200, budgets.text
    assert budgets.json()["llm"] == 3.5
    negative = await client.post(
        "/api/v1/system/budgets",
        json={"llm_daily_usd": -1, "api_daily_usd": 0},
        headers=auth_headers,
    )
    assert negative.status_code in {400, 422}


async def test_events_feed(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    events = await client.get("/api/v1/events", headers=auth_headers)
    assert events.status_code == 200
    assert "items" in events.json()


async def test_metrics_endpoint(client: AsyncClient, seeded: dict[str, Any]) -> None:
    metrics = await client.get("/metrics")
    assert metrics.status_code == 200
    assert "http_requests_total" in metrics.text
    assert "adapter_ok" in metrics.text


async def test_public_pages(client: AsyncClient) -> None:
    features = await client.get("/features?lang=ar")
    assert features.status_code == 200
    assert "ProcureAI" in features.text
    english = await client.get("/features?lang=en")
    assert english.status_code == 200
    assert "ProcureAI" in english.text
    bot = await client.get("/bot")
    assert bot.status_code == 200
    assert "MantiqOutreachBot" in bot.text
    ping = await client.get("/u-ping")
    assert ping.status_code == 200


async def test_security_headers_present(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in response.headers
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
