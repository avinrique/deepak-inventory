"""SQLAlchemy-backed AuditLogRepository — the first real writer of
app.models.audit_log.AuditLog (the model existed from the auth/authz phase
but nothing wrote to it yet).

``record`` is exposed as a plain module-level function taking an open
``Session`` (in addition to the ``SqlAuditLogRepository.record`` method
that opens its own) so callers that need the audit entry to commit or roll
back atomically with other writes — e.g.
app.repositories.sql.purchase_repository recording a goods receipt — can
add the row to their own transaction instead of it living in a separate
one that could succeed independently of the operation it's describing.
"""
import uuid

from sqlalchemy.orm import Session

from app.database.session import get_session
from app.models import AuditLog


def record_audit_log(db: Session, *, organization_id: uuid.UUID | None,
                     user_id: uuid.UUID | None, actor_email: str | None,
                     organization_name: str | None, action: str,
                     entity_type: str | None = None, entity_id: uuid.UUID | None = None,
                     changes: dict | None = None) -> None:
    db.add(AuditLog(organization_id=organization_id, user_id=user_id, actor_email=actor_email,
                    organization_name=organization_name, action=action,
                    entity_type=entity_type, entity_id=entity_id, changes=changes))


class SqlAuditLogRepository:
    def record(self, *, organization_id: uuid.UUID | None, user_id: uuid.UUID | None,
              actor_email: str | None, organization_name: str | None, action: str,
              entity_type: str | None = None, entity_id: uuid.UUID | None = None,
              changes: dict | None = None) -> None:
        with get_session() as db:
            record_audit_log(db, organization_id=organization_id, user_id=user_id,
                            actor_email=actor_email, organization_name=organization_name,
                            action=action, entity_type=entity_type, entity_id=entity_id,
                            changes=changes)
