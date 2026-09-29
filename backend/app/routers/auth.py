import re
from datetime import timedelta
from urllib.parse import urlencode

import jwt
import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..deps import current_user, get_db, get_owned
from ..models import SocialLink, User, UserProfile, utcnow
from ..schemas import (
    ChangePasswordIn, LoginIn, ProfileIn, ProfileOut, RefreshIn, RegisterIn, ResetConfirmIn,
    ResetRequestIn, SocialLinkIn, SocialLinkOut, UserOut,
)
from ..security import (
    hash_password, make_token, password_fingerprint, password_problems, read_token, token_pair,
    verify_password,
)
from ..services.mailer import send_mail

router = APIRouter(prefix="/api/authusers", tags=["auth"])
token_router = APIRouter(prefix="/api", tags=["auth"])
social_router = APIRouter(prefix="/auth/social", tags=["auth"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def get_profile(db: Session, user: User) -> UserProfile:
    profile = db.scalar(select(UserProfile).where(UserProfile.user_id == user.id))
    if not profile:
        profile = UserProfile(user_id=user.id)
        db.add(profile)
        db.commit()
    return profile


def auth_response(user):
    return {"user": UserOut.model_validate(user).model_dump(), **token_pair(user.id)}


@router.post("/register", status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    errors = {}
    if not EMAIL_RE.match(email):
        errors["email"] = ["Enter a valid email address."]
    elif db.scalar(select(User.id).where(func.lower(User.email) == email)):
        errors["email"] = ["An account with this email already exists."]
    if db.scalar(select(User.id).where(func.lower(User.username) == body.username.lower())):
        errors["username"] = ["That username is already taken."]
    if problems := password_problems(body.password, body.username, email):
        errors["password"] = problems
    if errors:
        raise HTTPException(400, errors)

    user = User(username=body.username, email=email, first_name=body.first_name, last_name=body.last_name,
                password=hash_password(body.password))
    db.add(user)
    db.flush()
    profile_data = body.model_dump(include=set(ProfileIn.model_fields), exclude_none=True)
    db.add(UserProfile(user_id=user.id, **profile_data))
    db.commit()
    return auth_response(user)


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    ident = body.username.strip()
    col = func.lower(User.email) if "@" in ident else User.username
    user = db.scalar(select(User).where(col == (ident.lower() if "@" in ident else ident)))
    if not user or not user.is_active or not verify_password(body.password, user.password):
        raise HTTPException(401, "Invalid username or password.")
    user.last_login = utcnow()
    db.commit()
    return auth_response(user)


@router.post("/logout")
def logout(user: User = Depends(current_user)):
    # Stateless JWTs: the client discards its tokens.
    return {"message": "Logged out."}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user


@router.get("/profile", response_model=ProfileOut)
def read_profile(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return get_profile(db, user)


@router.patch("/profile", response_model=ProfileOut)
def update_profile(body: ProfileIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    profile = get_profile(db, user)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(profile, k, v if v is not None or k == "date_of_birth" else "")
    db.commit()
    return profile


@router.post("/change-password")
def change_password(body: ChangePasswordIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    errors = {}
    if not verify_password(body.old_password, user.password):
        errors["old_password"] = ["Your old password was entered incorrectly."]
    if body.new_password1 != body.new_password2:
        errors["new_password2"] = ["The two password fields didn't match."]
    elif problems := password_problems(body.new_password1, user.username, user.email):
        errors["new_password2"] = problems
    if errors:
        raise HTTPException(400, errors)
    user.password = hash_password(body.new_password1)
    db.commit()
    return {"message": "Password changed successfully."}


@router.post("/password-reset")
def password_reset(body: ResetRequestIn, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    for user in db.scalars(select(User).where(func.lower(User.email) == email, User.is_active.is_(True))):
        token = make_token("reset", user.id, timedelta(hours=24), pwh=password_fingerprint(user.password))
        link = f"{settings.frontend_url}/reset-password.html?token={token}"
        send_mail(user.email, "LSuite — Password Reset", (
            f"Hi {user.username},\n\nYou requested a password reset for your LSuite account.\n\n"
            f"Set a new password here:\n{link}\n\nThis link expires in 24 hours. "
            "If you didn't request this, ignore this email.\n\n— LSuite\n"
        ))
    # Same answer either way so account existence doesn't leak.
    return {"message": "If that email exists, a reset link has been sent."}


@router.post("/password-reset/confirm")
def password_reset_confirm(body: ResetConfirmIn, db: Session = Depends(get_db)):
    try:
        payload = read_token(body.token, "reset")
    except jwt.PyJWTError:
        raise HTTPException(400, "Invalid or expired reset link.")
    user = db.get(User, int(payload["sub"]))
    if not user or payload.get("pwh") != password_fingerprint(user.password):
        raise HTTPException(400, "Invalid or expired reset link.")
    if problems := password_problems(body.new_password, user.username, user.email):
        raise HTTPException(400, {"new_password": problems})
    user.password = hash_password(body.new_password)
    db.commit()
    return {"message": "Password has been reset."}


@router.get("/links", response_model=list[SocialLinkOut])
def list_links(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(SocialLink).where(SocialLink.user_id == user.id).order_by(SocialLink.platform)).all()


@router.post("/links", response_model=SocialLinkOut, status_code=201)
def add_link(body: SocialLinkIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    link = SocialLink(user_id=user.id, platform=body.platform, url=body.url)
    db.add(link)
    db.commit()
    return link


@router.get("/links/{link_id}", response_model=SocialLinkOut)
def get_link(link_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return get_owned(db, SocialLink, link_id, user)


@router.patch("/links/{link_id}", response_model=SocialLinkOut)
def update_link(link_id: int, body: SocialLinkIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    link = get_owned(db, SocialLink, link_id, user)
    link.platform, link.url = body.platform, body.url
    db.commit()
    return link


@router.delete("/links/{link_id}", status_code=204)
def delete_link(link_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(get_owned(db, SocialLink, link_id, user))
    db.commit()


@token_router.post("/token/refresh")
def refresh_token(body: RefreshIn, db: Session = Depends(get_db)):
    try:
        payload = read_token(body.refresh, "refresh")
    except jwt.PyJWTError:
        raise HTTPException(401, "Token is invalid or expired.")
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(401, "User not found or inactive.")
    return {"access": token_pair(user.id)["access"]}


# ── Social login (Google / GitHub / Facebook) ───────────────────────────────
# Browser does a top-level redirect to the provider, comes back to this API,
# and gets sent on to the frontend with JWTs in the URL fragment.

PROVIDERS = {
    "google": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scope": "openid email profile",
        "creds": lambda: (settings.google_client_id, settings.google_client_secret),
    },
    "github": {
        "authorize": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token",
        "scope": "read:user user:email",
        "creds": lambda: (settings.github_client_id, settings.github_client_secret),
    },
    "facebook": {
        "authorize": "https://www.facebook.com/v19.0/dialog/oauth",
        "token": "https://graph.facebook.com/v19.0/oauth/access_token",
        "scope": "email,public_profile",
        "creds": lambda: (settings.facebook_app_id, settings.facebook_app_secret),
    },
}


def _api_base(request: Request):
    base = settings.api_url or str(request.base_url).rstrip("/")
    if "localhost" not in base and "127.0.0.1" not in base:
        base = base.replace("http://", "https://", 1)
    return base


def _provider(name):
    p = PROVIDERS.get(name)
    if not p or not p["creds"]()[0]:
        raise HTTPException(404, f"Social login provider '{name}' is not configured.")
    return p


def _fail(reason):
    return RedirectResponse(f"{settings.frontend_url}/login.html?error={reason}", status_code=302)


@social_router.get("/{provider}/login")
def social_login(provider: str, request: Request):
    p = _provider(provider)
    client_id, _ = p["creds"]()
    state = make_token("social", provider, timedelta(minutes=10))
    params = {
        "client_id": client_id,
        "redirect_uri": f"{_api_base(request)}/auth/social/{provider}/callback",
        "response_type": "code",
        "scope": p["scope"],
        "state": state,
    }
    return RedirectResponse(f"{p['authorize']}?{urlencode(params)}", status_code=302)


def _fetch_identity(provider, access_token):
    h = {"Authorization": f"Bearer {access_token}"}
    if provider == "google":
        d = requests.get("https://openidconnect.googleapis.com/v1/userinfo", headers=h, timeout=15).json()
        return {"email": d.get("email"), "first_name": d.get("given_name", ""), "last_name": d.get("family_name", ""),
                "username": (d.get("email") or "").split("@")[0]}
    if provider == "github":
        d = requests.get("https://api.github.com/user", headers=h, timeout=15).json()
        email = d.get("email")
        if not email:
            emails = requests.get("https://api.github.com/user/emails", headers=h, timeout=15).json()
            email = next((e["email"] for e in emails if e.get("primary") and e.get("verified")), None)
        first, _, last = (d.get("name") or "").partition(" ")
        return {"email": email, "first_name": first, "last_name": last, "username": d.get("login", ""),
                "github_url": d.get("html_url", ""), "portfolio_url": d.get("blog", "")}
    d = requests.get("https://graph.facebook.com/me", params={"fields": "id,email,first_name,last_name",
                     "access_token": access_token}, timeout=15).json()
    return {"email": d.get("email"), "first_name": d.get("first_name", ""), "last_name": d.get("last_name", ""),
            "username": f"fb{d.get('id', '')}"}


def _unique_username(db, base):
    base = re.sub(r"[^\w.@+-]", "", base or "user")[:140] or "user"
    name, n = base, 1
    while db.scalar(select(User.id).where(User.username == name)):
        n += 1
        name = f"{base}{n}"
    return name


@social_router.get("/{provider}/callback")
def social_callback(provider: str, request: Request, code: str = "", state: str = "", db: Session = Depends(get_db)):
    p = _provider(provider)
    try:
        if read_token(state, "social")["sub"] != provider:
            return _fail("invalid_state")
    except jwt.PyJWTError:
        return _fail("invalid_state")
    if not code:
        return _fail("oauth_failed")

    client_id, secret = p["creds"]()
    r = requests.post(p["token"], data={
        "client_id": client_id, "client_secret": secret, "code": code, "grant_type": "authorization_code",
        "redirect_uri": f"{_api_base(request)}/auth/social/{provider}/callback",
    }, headers={"Accept": "application/json"}, timeout=20)
    access = r.json().get("access_token") if r.ok else None
    if not access:
        return _fail("oauth_failed")

    ident = _fetch_identity(provider, access)
    email = (ident.get("email") or "").lower()
    if not email:
        return _fail("no_email")

    user = db.scalar(select(User).where(func.lower(User.email) == email))
    if not user:
        user = User(username=_unique_username(db, ident["username"]), email=email,
                    first_name=ident["first_name"][:150], last_name=ident["last_name"][:150], password="!")
        db.add(user)
        db.flush()
    if not user.is_active:
        return _fail("inactive")
    profile = get_profile(db, user)
    if ident.get("github_url") and not profile.github_url:
        profile.github_url = ident["github_url"][:200]
    if ident.get("portfolio_url") and not profile.portfolio_url:
        profile.portfolio_url = ident["portfolio_url"][:200]
    user.last_login = utcnow()
    db.commit()

    tokens = token_pair(user.id)
    page = "/dashboard.html" if profile.occupation and profile.city else "/complete-profile.html"
    return RedirectResponse(
        f"{settings.frontend_url}{page}#access={tokens['access']}&refresh={tokens['refresh']}", status_code=302
    )


@social_router.get("/providers")
def social_providers():
    return {"providers": [name for name, p in PROVIDERS.items() if p["creds"]()[0]]}
