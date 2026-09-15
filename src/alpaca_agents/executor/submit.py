"""The single place a prepared order body meets the broker. One attempt, ever.

submit_claimed() is the only caller of PaperClient.submit_order(). It requires:
- a claim/prepare result the journal produced (approved, prepared_order, authorization_id)
- an explicit submit=True from the controller (default False; the journal's own
  submission_enabled flag is always False and is not consulted)

Outcome mapping, all journaled before this function returns:
- 2xx with a record whose client_order_id echoes ours -> mark_submitted (stays claimed/live)
- broker-answered 400/403/422 with a request id      -> resolve_unplaced (slot released)
- anything else (timeout, 5xx, bad body, mismatch)   -> note_submit_outcome_unknown (stays claimed;
                                                        reconciliation matches by client_order_id)
"""
from datetime import datetime

from .client import ExecutorError, validate_order_body


def submit_claimed(client, journal, claim_result: dict, *, now: datetime, submit: bool = False) -> dict:
    if submit is not True:
        return {"outcome": "not_submitted", "reason": "SUBMIT_DISABLED: controller did not enable submission"}
    if not isinstance(claim_result, dict) or claim_result.get("approved") is not True:
        return {"outcome": "not_submitted", "reason": "NOT_APPROVED"}
    authorization_id = claim_result.get("authorization_id")
    body = claim_result.get("prepared_order")
    if not isinstance(authorization_id, str) or not isinstance(body, dict):
        return {"outcome": "not_submitted", "reason": "MALFORMED_CLAIM"}
    # The journal's stored body is what gets sent. The caller's copy must match
    # it exactly; it is never trusted on its own. A claimed-and-sent or
    # non-claimed intent has no sendable body, so a second call cannot resend.
    stored = journal.sendable_body(authorization_id)
    if stored is None:
        return {"outcome": "not_submitted", "reason": "INTENT_NOT_SENDABLE: not claimed or already sent"}
    if body != stored:
        return {"outcome": "not_submitted", "reason": "BODY_MISMATCH: claim result differs from journal"}
    try:
        body = validate_order_body(stored)
    except ExecutorError as exc:
        return {"outcome": "not_submitted", "reason": f"BODY_REJECTED: {exc}"}
    if body["client_order_id"] != "paper-" + authorization_id:
        return {"outcome": "not_submitted", "reason": "CLIENT_ID_MISMATCH"}
    try:
        record = client.submit_order(body)
    except ExecutorError as exc:
        if exc.request_id and exc.status in journal.UNPLACED_STATUSES:
            journal.resolve_unplaced(authorization_id, http_status=exc.status, request_id=exc.request_id, now=now)
            return {"outcome": "unplaced", "reason": str(exc), "status": exc.status}
        journal.note_submit_outcome_unknown(authorization_id, local_id=exc.local_id or "unknown", now=now)
        return {"outcome": "unknown", "reason": str(exc), "local_id": exc.local_id}
    broker_id, echoed, status = record.get("id"), record.get("client_order_id"), record.get("status")
    if (not isinstance(broker_id, str) or not broker_id or echoed != body["client_order_id"]
            or not isinstance(status, str)):
        journal.note_submit_outcome_unknown(authorization_id, local_id="response-mismatch", now=now)
        return {"outcome": "unknown", "reason": "BROKER_RESPONSE_MISMATCH"}
    journal.mark_submitted(authorization_id, broker_order_id=broker_id, broker_status=status, now=now)
    return {"outcome": "submitted", "broker_order_id": broker_id, "broker_status": status}
