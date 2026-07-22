"""JWT authentication & user management for the Fiber360 Survey API."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

try:
    import jwt
except ImportError:
    jwt = None  # type: ignore
try:
    from passlib.context import CryptContext
except ImportError:
    CryptContext = None  # type: ignore

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from database import get_conn

# ── Config ──

SECRET_KEY = os.environ.get(
    "SURVEY_JWT_SECRET",
    "fiber360-survey-dev-secret-change-in-production",
)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(
    os.environ.get("SURVEY_JWT_EXPIRE_MINUTES", "1440")  # 24 hours
)

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto") if CryptContext else None

bearer_scheme = HTTPBearer(auto_error=False)


# ── Password helpers ──


def hash_password(password: str) -> str:
    if _pwd is None:
        raise RuntimeError("passlib is not available. Install with: pip install passlib[bcrypt]")
    return _pwd.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    if _pwd is None:
        raise RuntimeError("passlib is not available. Install with: pip install passlib[bcrypt]")
    return _pwd.verify(plain, hashed)


# ── Token helpers ──


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    if jwt is None:
        raise RuntimeError("PyJWT is not available. Install with: pip install pyjwt")
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Dict[str, Any]:
    if jwt is None:
        raise RuntimeError("PyJWT is not available. Install with: pip install pyjwt")
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


# ── User DB operations ──


def init_users_table() -> None:
    """Create the users table in the business schema if it doesn't exist."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE SCHEMA IF NOT EXISTS business
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS business.users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                hashed_password TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'engineer',
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # Add seed demo user if not exists
        hashed = hash_password("demo1234")
        cur.execute(
            """
            INSERT INTO business.users (id, email, name, hashed_password, role)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (email) DO NOTHING
            """,
            (
                "demo-user",
                "surveyor@fiber360.io",
                "Demo Engineer",
                hashed,
                "engineer",
            ),
        )


def create_user(email: str, name: str, password: str, role: str = "engineer") -> Dict[str, Any]:
    conn = get_conn()
    user_id = f"user_{uuid4_hex()}"
    hashed = hash_password(password)
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO business.users (id, email, name, hashed_password, role)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, email, name, role, created_at
                """,
                (user_id, email, name, hashed, role),
            )
            row = cur.fetchone()
            return {
                "id": row[0],
                "email": row[1],
                "name": row[2],
                "role": row[3],
                "created_at": row[4].isoformat() if row[4] else None,
            }
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A user with this email already exists",
                )
            raise


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, email, name, hashed_password, role, is_active
            FROM business.users
            WHERE email = %s
            """,
            (email,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "email": row[1],
        "name": row[2],
        "hashed_password": row[3],
        "role": row[4],
        "is_active": row[5],
    }


def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, email, name, role, is_active, created_at
            FROM business.users
            WHERE id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "email": row[1],
        "name": row[2],
        "role": row[3],
        "is_active": row[4],
        "created_at": row[5].isoformat() if row[5] else None,
    }


# ── Dependency ──


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> Dict[str, Any]:
    """FastAPI dependency that extracts and validates the JWT, returning the user."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    payload = decode_token(credentials.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    user = get_user_by_id(user_id)
    if not user or not user.get("is_active"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or deactivated",
        )
    return user


# ── Helpers ──


def uuid4_hex() -> str:
    import uuid
    return uuid.uuid4().hex
