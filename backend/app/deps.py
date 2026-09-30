from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import Client, User
from .security import read_token


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "Authentication credentials were not provided.")
    try:
        payload = read_token(header[7:].strip(), "access")
    except jwt.PyJWTError:
        raise HTTPException(401, "Token is invalid or expired.")
    user = db.scalar(select(User).where(User.id == int(payload["sub"])))
    if not user or not user.is_active:
        raise HTTPException(401, "User not found or inactive.")
    return user


def owner(user: User = Depends(current_user)) -> User:
    if not user.is_owner:
        raise HTTPException(403, "Only the practice owner can do this.")
    return user


@dataclass
class Ctx:
    user: User
    client: Client

    @property
    def client_id(self):
        return self.client.id


def client_ctx(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Ctx:
    """The client being worked on, from the X-Client-Id header the frontend sends."""
    raw = request.headers.get("x-client-id") or request.query_params.get("client_id")
    if not raw or not raw.isdigit():
        raise HTTPException(400, "Select a client first.")
    client = db.get(Client, int(raw))
    if not client or client.practice_id != user.practice_id:
        raise HTTPException(404, "Client not found.")
    return Ctx(user, client)


def get_owned(db: Session, model, obj_id, scope):
    """Fetch a row that must belong to the given client (Ctx) or practice (User)."""
    obj = db.get(model, obj_id) if obj_id is not None else None
    if isinstance(scope, Ctx):
        ok = obj is not None and getattr(obj, "client_id", None) == scope.client_id
    else:
        ok = obj is not None and getattr(obj, "practice_id", None) == scope.practice_id
    if not ok:
        raise HTTPException(404, "Not found.")
    return obj
