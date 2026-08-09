from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from src.dependencies import DbSession

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/health/live")
def liveness():
    return {"status": "alive"}


@router.get("/health/ready")
def readiness(request: Request, db: DbSession):
    mesh = request.app.state.mesh
    checks = {
        "database": "ok",
        "schema": "ok",
        "mesh": "available" if mesh.available else "disabled" if mesh.configured else "not_configured",
        "pinecone": "configured" if request.app.state.vectors.available else "not_configured",
    }
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - readiness must translate any driver failure into status
        checks["database"] = "failed"
    if checks["database"] == "ok":
        try:
            db.execute(text("SELECT skill_bundle, content_sections FROM products LIMIT 0"))
            db.execute(text(
                "SELECT product_id, source_product_id, purchased_at FROM user_enrollments LIMIT 0"
            ))
        except Exception:  # noqa: BLE001 - readiness reports pending migrations without leaking details
            db.rollback()
            checks["schema"] = "migration_required"
    ready = checks["database"] == checks["schema"] == "ok" and (
        request.app.state.settings.app_env != "production"
        or (mesh.available and checks["pinecone"] == "configured")
    )
    return JSONResponse(
        {"status": "ready" if ready else "degraded", "checks": checks},
        status_code=200 if ready else 503,
    )
