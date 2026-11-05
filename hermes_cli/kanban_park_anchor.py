"""Read-only owner park anchors shared by promotion and respawn guards."""
import logging
import sqlite3

log = logging.getLogger(__name__)


def active_park_anchor(conn: sqlite3.Connection, task_id: str):
    """Return newest unreleased anchor id, logging its id and timestamp.

    A release must be a company comment later than every matching anchor.
    Timestamp ties remain guarded. Comment bodies are never logged.
    """
    from hermes_cli import kanban_db as kb

    rows = conn.execute(
        "SELECT id, author, body, created_at FROM task_comments "
        "WHERE task_id = ? AND author IN ('default', 'company')",
        (task_id,),
    ).fetchall()
    anchors = []
    releases = []
    for row in rows:
        body = kb._lossy_text(row['body']) or ''
        when = kb._to_epoch(row['created_at'])
        if row['author'] == 'company' and body.startswith('COMPANY RELEASE'):
            releases.append(when)
        elif any(word in body.lower() for word in ('park', 'hold', 'frozen')):
            anchors.append((row['id'], when))
    if not anchors:
        return None
    # An unreadable time cannot prove a release is later: keep the card parked.
    if any(when is None for _, when in anchors):
        anchor = next(pair for pair in anchors if pair[1] is None)
    else:
        anchor = max(anchors, key=lambda pair: (pair[1], pair[0]))
        if any(when is not None and when > anchor[1] for when in releases):
            return None
    log.warning('kanban park guard: task %s anchor comment %s timestamp %s', task_id, *anchor)
    return anchor[0]
