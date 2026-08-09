from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from src.config import get_settings
from src.database import SessionLocal
from src.models import Role, User
from src.security import (
    create_session_token,
    hash_password,
    validate_csrf,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["authentication"])


def set_session(response: Response, user: User) -> None:
    settings = get_settings()
    response.set_cookie(
        "session", create_session_token(user.id, user.role.value), max_age=604800,
        httponly=True, secure=settings.session_cookie_secure, samesite="lax", path="/",
    )


@router.post("/register")
def register(request: Request, email: str = Form(...), password: str = Form(...), csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    if len(password) < 10:
        return RedirectResponse("/login?error=Password+must+be+at+least+10+characters", status_code=303)
    with SessionLocal() as db:
        normalized = email.strip().lower()
        if db.scalar(select(User).where(User.email == normalized)):
            return RedirectResponse("/login?error=Account+already+exists", status_code=303)
        user = User(email=normalized, password_hash=hash_password(password), role=Role.user)
        db.add(user)
        db.commit()
        response = RedirectResponse("/discover", status_code=303)
        set_session(response, user)
        return response


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email.strip().lower()))
        if not user or not verify_password(password, user.password_hash):
            return RedirectResponse("/login?error=Invalid+email+or+password", status_code=303)
        user.last_login_at = datetime.now(timezone.utc)
        db.commit()
        response = RedirectResponse("/admin" if user.role == Role.admin else "/discover", status_code=303)
        set_session(response, user)
        return response


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("session", path="/")
    return response

