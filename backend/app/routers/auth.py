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
from ..deps import current_user, get_db, owner
from ..models import AccessRequest, Practice, User, utcnow
from ..schemas import (
    AccessRequestIn, ChangePasswordIn, LoginIn, MeIn, MemberIn, PracticeIn, PracticeOut, RefreshIn, RegisterIn, ResetConfirmIn,
    ResetRequestIn, UserOut,
)
from ..security import (
    hash_password, make_token, password_fingerprint, password_problems, read_token, token_pair, verify_password,
)
from ..services.categorize import seed_categories
from ..services.mailer import send_mail
from ..services.signup_lock import signup_problem

router = APIRouter(prefix="/api/auth", tags=["auth"])
practice_router = APIRouter(prefix="/api/practice", tags=["practice"])
social_router = APIRouter(prefix="/auth/social", tags=["auth"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def auth_response(user):
    return {"user": UserOut.model_validate(user).model_dump(), **token_pair(user.id)}


def validate_new_user(db: Session, email: str, username: str, password: str):
    errors = {}
    if not EMAIL_RE.match(email):
        errors["email"] = ["Enter a valid email address."]
    elif db.scalar(select(User.id).where(func.lower(User.email) == email)):
        errors["email"] = ["An account with this email already exists."]
    elif problem := signup_problem(email):
        errors["email"] = [problem]
    if db.scalar(select(User.id).where(func.lower(User.username) == username.lower())):
        errors["username"] = ["That username is already taken."]
    if problems := password_problems(password, username, email):
        errors["password"] = problems
    if errors:
        raise HTTPException(400, errors)


@router.post("/register", status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    """Creates a new organisation with this user as its owner."""
    email = body.email.strip().lower()
    validate_new_user(db, email, body.username, body.password)
    practice = Practice(name=body.practice_name.strip() or f"{body.first_name} {body.last_name}".strip())
    db.add(practice)
    db.flush()
    seed_categories(db, practice.id)
    user = User(practice_id=practice.id, role="owner", username=body.username, email=email,
                first_name=body.first_name, last_name=body.last_name, password=hash_password(body.password))
    db.add(user)
    db.commit()
    return auth_response(user)


@router.post("/request-access", status_code=202)
def request_access(body: AccessRequestIn, db: Session = Depends(get_db)):
    """Emails the admin so they can verify this domain (or address) in SES, which opens sign-up for it."""
    from html import escape

    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, {"email": ["Enter a valid email address."]})
    if not signup_problem(email):
        return {"message": "This email can already sign up. Go ahead and register."}
    since = utcnow() - timedelta(hours=1)
    if db.scalar(select(AccessRequest.id).where(AccessRequest.email == email, AccessRequest.created_at > since)):
        return {"message": "We already have your request. We'll email you once you're in."}
    if (db.scalar(select(func.count()).select_from(AccessRequest)
                  .where(AccessRequest.created_at > utcnow() - timedelta(days=1))) or 0) >= 30:
        raise HTTPException(429, "Too many requests today. Try again tomorrow.")
    req = AccessRequest(email=email, name=body.name.strip(), company=body.company.strip(), message=body.message.strip())
    db.add(req)
    db.commit()
    if settings.admin_email:
        domain = email.rsplit("@", 1)[-1]
        console = f"https://{settings.ses_region}.console.aws.amazon.com/ses/home?region={settings.ses_region}#/identities"
        rows = [("Name", req.name), ("Email", email), ("Company", req.company or "-"), ("Domain", domain),
                ("Message", req.message or "-")]
        text_body = "\n".join(f"{k}: {v}" for k, v in rows) + (
            f"\n\nTo let them in, verify {domain} (or just {email}) in Amazon SES: {console}\n"
            "Sign-up opens for them within 10 minutes of verification.")
        html = ("<h2 style=\"color:#0b1f4d\">C.T.H.A.I access request</h2><table cellpadding=\"6\">"
                + "".join(f"<tr><td><b>{k}</b></td><td>{escape(v)}</td></tr>" for k, v in rows)
                + f"</table><p>To let them in, verify <b>{escape(domain)}</b> (or just {escape(email)}) in "
                  f"<a href=\"{console}\">Amazon SES</a>. Sign-up opens for them within 10 minutes.</p>")
        try:
            send_mail(settings.admin_email, f"Access request: {req.name} ({domain})", text_body, html=html)
        except Exception:
            pass  # kept in the table either way
    return {"message": "Request sent. We'll email you once your organisation is registered."}


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    ident = body.username.strip()
    cond = func.lower(User.email) == ident.lower() if "@" in ident else User.username == ident
    user = db.scalar(select(User).where(cond))
    if not user or not user.is_active or not verify_password(body.password, user.password):
        raise HTTPException(401, "Invalid username or password.")
    user.last_login = utcnow()
    db.commit()
    return auth_response(user)


@router.post("/logout")
def logout(user: User = Depends(current_user)):
    return {"message": "Logged out."}


@router.post("/token/refresh")
def refresh_token(body: RefreshIn, db: Session = Depends(get_db)):
    try:
        payload = read_token(body.refresh, "refresh")
    except jwt.PyJWTError:
        raise HTTPException(401, "Token is invalid or expired.")
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(401, "User not found or inactive.")
    return {"access": token_pair(user.id)["access"]}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user


@router.patch("/me", response_model=UserOut)
def update_me(body: MeIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    data = body.model_dump(exclude_none=True)
    if "email" in data:
        data["email"] = data["email"].strip().lower()
        if not EMAIL_RE.match(data["email"]):
            raise HTTPException(400, {"email": ["Enter a valid email address."]})
        if db.scalar(select(User.id).where(func.lower(User.email) == data["email"], User.id != user.id)):
            raise HTTPException(400, {"email": ["An account with this email already exists."]})
    for k, v in data.items():
        setattr(user, k, v)
    db.commit()
    return user


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
        send_mail(user.email, "C.T.H.A.I — Password Reset", (
            f"Hi {user.first_name or user.username},\n\nYou requested a password reset.\n\n"
            f"Set a new password here:\n{link}\n\nThis link expires in 24 hours. "
            "If you didn't request this, ignore this email.\n"
        ))
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


# ── Organisation + team ─────────────────────────────────────────────────────

@practice_router.get("", response_model=PracticeOut)
def get_practice(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.get(Practice, user.practice_id)


@practice_router.patch("", response_model=PracticeOut)
def update_practice(body: PracticeIn, user: User = Depends(owner), db: Session = Depends(get_db)):
    practice = db.get(Practice, user.practice_id)
    practice.name = body.name.strip()
    db.commit()
    return practice


@practice_router.get("/users", response_model=list[UserOut])
def team(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(User).where(User.practice_id == user.practice_id, User.is_active.is_(True))
                      .order_by(User.first_name)).all()


@practice_router.post("/users", response_model=UserOut, status_code=201)
def add_member(body: MemberIn, user: User = Depends(owner), db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    validate_new_user(db, email, body.username, body.password)
    member = User(practice_id=user.practice_id, role="owner" if body.role == "owner" else "bookkeeper",
                  username=body.username, email=email, first_name=body.first_name, last_name=body.last_name,
                  password=hash_password(body.password))
    db.add(member)
    db.commit()
    return member


@practice_router.delete("/users/{user_id}", status_code=204)
def remove_member(user_id: int, user: User = Depends(owner), db: Session = Depends(get_db)):
    member = db.get(User, user_id)
    if not member or member.practice_id != user.practice_id:
        raise HTTPException(404, "Not found.")
    if member.id == user.id:
        raise HTTPException(400, "You can't remove yourself.")
    member.is_active = False
    db.commit()


# ── Google sign-in (existing accounts only) ─────────────────────────────────
# Top-level redirect to Google, back to this API, then on to the frontend with JWTs in the fragment.

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"


def _api_base(request: Request):
    base = settings.api_url or str(request.base_url).rstrip("/")
    if "localhost" not in base and "127.0.0.1" not in base:
        base = base.replace("http://", "https://", 1)
    return base


def _fail(reason):
    return RedirectResponse(f"{settings.frontend_url}/login.html?error={reason}", status_code=302)


@social_router.get("/providers")
def providers():
    return {"providers": ["google"] if settings.google_client_id else []}


@social_router.get("/google/login")
def google_login(request: Request):
    if not settings.google_client_id:
        raise HTTPException(404, "Google sign-in is not configured.")
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": f"{_api_base(request)}/auth/social/google/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "state": make_token("social", "google", timedelta(minutes=10)),
    }
    return RedirectResponse(f"{GOOGLE_AUTH}?{urlencode(params)}", status_code=302)


@social_router.get("/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", db: Session = Depends(get_db)):
    try:
        read_token(state, "social")
    except jwt.PyJWTError:
        return _fail("invalid_state")
    if not code:
        return _fail("oauth_failed")
    r = requests.post(GOOGLE_TOKEN, data={
        "client_id": settings.google_client_id, "client_secret": settings.google_client_secret, "code": code,
        "grant_type": "authorization_code", "redirect_uri": f"{_api_base(request)}/auth/social/google/callback",
    }, timeout=20)
    access = r.json().get("access_token") if r.ok else None
    if not access:
        return _fail("oauth_failed")
    info = requests.get("https://openidconnect.googleapis.com/v1/userinfo",
                        headers={"Authorization": f"Bearer {access}"}, timeout=15).json()
    email = (info.get("email") or "").lower()
    user = db.scalar(select(User).where(func.lower(User.email) == email)) if email else None
    # Team members are added by the owner; Google sign-in never creates accounts.
    if not user or not user.is_active:
        return _fail("no_account")
    user.last_login = utcnow()
    db.commit()
    tokens = token_pair(user.id)
    return RedirectResponse(f"{settings.frontend_url}/dashboard.html#access={tokens['access']}&refresh={tokens['refresh']}",
                            status_code=302)
