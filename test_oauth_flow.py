"""End-to-end OAuth 2.1 + MCP smoke test against a running authed server.

Exercises: discovery -> DCR -> /authorize -> /login (password) -> /token (PKCE)
-> authenticated /mcp call, plus negative checks (no token, bad password).
"""

import base64
import hashlib
import os
import secrets
import sys

import httpx

BASE = os.environ.get("BRAIN_MCP_PUBLIC_URL", "http://127.0.0.1:8861")
PASSWORD = os.environ.get("BRAIN_MCP_PASSWORD", "hunter2-test")
REDIRECT = "http://127.0.0.1:9999/cb"


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


def main() -> int:
    c = httpx.Client(follow_redirects=False, timeout=10)

    # 0. discovery
    meta = c.get(f"{BASE}/.well-known/oauth-authorization-server").json()
    assert meta["authorization_endpoint"] == f"{BASE}/authorize", meta
    print("discovery OK:", meta["issuer"])

    # 1. unauthenticated MCP call must be rejected
    r = c.post(f"{BASE}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
               headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401, f"expected 401, got {r.status_code}"
    print("no-token /mcp -> 401 OK")

    # 2. dynamic client registration
    reg = c.post(f"{BASE}/register", json={
        "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": "test-client",
        "scope": "inventory",
    }).json()
    client_id = reg["client_id"]
    print("DCR OK, client_id:", client_id[:12], "...")

    # 3. /authorize -> redirect to /login
    verifier, challenge = pkce()
    r = c.get(f"{BASE}/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "state": "xyz", "scope": "inventory",
    })
    assert r.status_code in (302, 307), f"authorize -> {r.status_code}: {r.text[:200]}"
    login_url = r.headers["location"]
    assert "/login?" in login_url, login_url
    login_id = login_url.split("login_id=")[1]
    print("authorize -> /login redirect OK")

    # 4a. wrong password -> 401, no code
    r = c.post(f"{BASE}/login", data={"login_id": login_id, "password": "wrong"})
    assert r.status_code == 401, f"bad pw expected 401, got {r.status_code}"
    print("bad password -> 401 OK")

    # 4b. correct password -> redirect to client callback with code
    r = c.post(f"{BASE}/login", data={"login_id": login_id, "password": PASSWORD})
    assert r.status_code == 302, f"login expected 302, got {r.status_code}: {r.text[:200]}"
    cb = r.headers["location"]
    assert cb.startswith(REDIRECT) and "code=" in cb and "state=xyz" in cb, cb
    code = cb.split("code=")[1].split("&")[0]
    print("login -> code OK")

    # 5. token exchange with PKCE verifier
    tok = c.post(f"{BASE}/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": verifier,
    }).json()
    assert "access_token" in tok and "refresh_token" in tok, tok
    access, refresh = tok["access_token"], tok["refresh_token"]
    print("token exchange OK, expires_in:", tok.get("expires_in"))

    # 6. wrong PKCE verifier must fail (replay another code)
    r2 = c.get(f"{BASE}/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": "inventory",
    })
    lid2 = r2.headers["location"].split("login_id=")[1]
    cb2 = c.post(f"{BASE}/login", data={"login_id": lid2, "password": PASSWORD}).headers["location"]
    code2 = cb2.split("code=")[1].split("&")[0]
    bad = c.post(f"{BASE}/token", data={
        "grant_type": "authorization_code", "code": code2, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": "wrong-verifier",
    })
    assert bad.status_code == 400, f"bad PKCE expected 400, got {bad.status_code}"
    print("bad PKCE verifier -> rejected OK")

    # 7. authenticated MCP initialize
    init = c.post(f"{BASE}/mcp",
        headers={"Authorization": f"Bearer {access}",
                 "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "0"}}})
    assert init.status_code == 200, f"authed init expected 200, got {init.status_code}: {init.text[:200]}"
    print("authed /mcp initialize -> 200 OK")

    # 8. refresh token rotation
    rt = c.post(f"{BASE}/token", data={
        "grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id,
    }).json()
    assert "access_token" in rt and rt["access_token"] != access, rt
    print("refresh rotation OK")

    print("\nALL OAUTH E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
