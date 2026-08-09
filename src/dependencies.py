from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from src.database import get_db
from src.models import User
from src.security import read_session_token

DbSession = Annotated[Session, Depends(get_db)]


def get_optional_user(request: Request, db: DbSession) -> User | None:
    token = request.cookies.get("session")
    payload = read_session_token(token) if token else None
    return db.get(User, payload.get("sub")) if payload else None


def require_user(user: Annotated[User | None, Depends(get_optional_user)]) -> User:
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user


def require_admin(user: Annotated[User, Depends(require_user)]) -> User:
    if user.role.value != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required")
    return user


CurrentUser = Annotated[User, Depends(require_user)]
CurrentAdmin = Annotated[User, Depends(require_admin)]
