from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models import Event
from src.observability.metrics import EVENT_DUPLICATES
from src.schemas.events import EventIn


def ingest_events(db: Session, user_id: str, events: list[EventIn]) -> tuple[int, int]:
    rows = [{
        "event_id": item.event_id,
        "user_id": user_id,
        "session_id": item.session_id,
        "event_type": item.event_type,
        "product_id": item.product_id,
        "search_query": item.search_query,
        "metadata": item.metadata,
        "occurred_at": item.occurred_at,
    } for item in events]
    dialect = db.get_bind().dialect.name
    try:
        if dialect in {"postgresql", "sqlite"}:
            insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
            statement = insert(Event.__table__).values(rows).on_conflict_do_nothing(
                index_elements=["event_id"]
            ).returning(Event.event_id)
            inserted = len(list(db.scalars(statement)))
            db.commit()
        else:
            existing = set(db.scalars(select(Event.event_id).where(
                Event.event_id.in_([item.event_id for item in events])
            )))
            for item in events:
                if item.event_id not in existing:
                    db.add(Event(
                        event_id=item.event_id, user_id=user_id, session_id=item.session_id,
                        event_type=item.event_type, product_id=item.product_id,
                        search_query=item.search_query, event_metadata=item.metadata,
                        occurred_at=item.occurred_at,
                    ))
            inserted = len(events) - len(existing)
            db.commit()
    except IntegrityError:
        db.rollback()
        inserted = 0
    duplicates = len(events) - inserted
    if duplicates:
        EVENT_DUPLICATES.inc(duplicates)
    return inserted, duplicates


def user_events(db: Session, user_id: str, limit: int = 500) -> list[Event]:
    return list(db.scalars(
        select(Event).where(Event.user_id == user_id).order_by(Event.occurred_at.desc()).limit(limit)
    ))


def delete_user_events(db: Session, user_id: str) -> int:
    result = db.execute(delete(Event).where(Event.user_id == user_id))
    db.commit()
    return int(result.rowcount or 0)
