# app/auth.py – with scope/role enforcement
import os, time, httpx, functools
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError

AUTH0_DOMAIN   = os.getenv("AUTH0_DOMAIN")
AUTH0_AUDIENCE = os.getenv("AUTH0_AUDIENCE") or os.getenv("AUTH0_API_AUDIENCE")
AZ_TENANT_ID   = os.getenv("AZURE_TENANT_ID")
AZ_AUDIENCE    = os.getenv("AZURE_AD_AUDIENCE") or os.getenv("AZ_AUDIENCE")

AUTH0_ISSUER = f"https://{AUTH0_DOMAIN}/"
AZ_ISSUER    = f"https://sts.windows.net/{AZ_TENANT_ID}/"

AUTH0_JWKS_URL = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"
AZ_JWKS_URL    = f"https://login.microsoftonline.com/{AZ_TENANT_ID}/discovery/v2.0/keys"

bearer_scheme = HTTPBearer(auto_error=False)
_jwks_cache: dict[str, tuple[list, float]] = {}     # {url: (keys, ts)}
_JWKS_TTL = 12 * 60 * 60                            # 12 h

async def _get_jwks(url: str):
    now = time.time()
    if url not in _jwks_cache or now - _jwks_cache[url][1] > _JWKS_TTL:
        async with httpx.AsyncClient(timeout=3) as c:
            res = await c.get(url)
        res.raise_for_status()
        _jwks_cache[url] = (res.json()["keys"], now)
    return _jwks_cache[url][0]

async def verify_jwt(
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    required_scope: Optional[str] = None,
    required_role: Optional[str]  = None,
):
    if not creds or creds.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing credentials")

    token = creds.credentials
    for issuer, jwks_url, aud in (
        (AUTH0_ISSUER, AUTH0_JWKS_URL, AUTH0_AUDIENCE),
        (AZ_ISSUER,   AZ_JWKS_URL,   AZ_AUDIENCE),
    ):
        try:
            keys = await _get_jwks(jwks_url)
            hdr  = jwt.get_unverified_header(token)
            key  = next(k for k in keys if k["kid"] == hdr["kid"])
            claims = jwt.decode(token, key, algorithms=[hdr["alg"]], audience=aud, issuer=issuer)

            # ── optional authorisation ─────────────────────────────
            if required_scope and required_scope not in claims.get("scope", "").split():
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                    detail=f"Missing scope '{required_scope}'")
            if required_role and required_role not in claims.get("roles", []):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                    detail=f"Missing role '{required_role}'")

            return claims
        except (JWTError, StopIteration):
            continue
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")