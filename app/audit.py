from flask import has_request_context, request
from flask_login import current_user

from .extensions import db
from .models import AuditLog


def audit(action, entite=None, entite_id=None, ancien=None, nouveau=None, user=None):
    """Ajoute une ligne au journal d'audit (le commit est fait par l'appelant)."""
    if user is None and has_request_context() and current_user.is_authenticated:
        user = current_user
    ip = request.remote_addr if has_request_context() else None
    db.session.add(
        AuditLog(
            user_id=user.id if user else None,
            action=action,
            entite=entite,
            entite_id=entite_id,
            ancien=None if ancien is None else str(ancien),
            nouveau=None if nouveau is None else str(nouveau),
            ip=ip,
        )
    )
