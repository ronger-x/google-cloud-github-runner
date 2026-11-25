"""
Route for the periodic reconcile pass (called by Cloud Scheduler).
"""
import hmac
import logging
import os
from flask import Blueprint, request, jsonify
from app.services.reconcile_service import ReconcileService

logger = logging.getLogger(__name__)

reconcile_bp = Blueprint('reconcile', __name__)

RECONCILE_TOKEN_HEADER = 'X-Reconcile-Token'


def _authorized():
    # The service is public (no IAM on Cloud Run): require a shared token. Without one
    # configured the endpoint is disabled rather than open
    expected = os.environ.get('RECONCILE_TOKEN', '')
    if not expected:
        return False
    provided = request.headers.get(RECONCILE_TOKEN_HEADER, '')
    return hmac.compare_digest(provided.encode('utf-8'), expected.encode('utf-8'))


@reconcile_bp.route('/reconcile', methods=['POST'])
def reconcile():
    """Create runners for stranded queued jobs and delete orphaned runner VMs."""
    if not _authorized():
        return jsonify({'status': 'forbidden'}), 403
    summary = ReconcileService().reconcile()
    status = 'error' if summary['errors'] and not (summary['created'] or summary['deleted']) else 'success'
    return jsonify({'status': status, **summary}), 200
