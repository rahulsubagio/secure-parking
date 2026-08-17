import json
import logging
import os
import sqlite3
import threading
from datetime import datetime

import config

log = logging.getLogger(__name__)


class MessageStore:
    """
    SQLite-backed persistent message queue with audit trail.

    Transactional MQTT messages (general visitor, member request) are
    stored here *before* being published.  If the publish succeeds the
    row is moved to ``message_history`` for audit trail; if it fails
    the row remains in ``pending_messages`` and a background retry
    worker picks it up later.

    The ``help`` topic is intentionally excluded — it is real-time and
    must not be queued.

    Thread safety is provided through a reentrant lock that serialises
    all database access.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS pending_messages (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        topic           TEXT    NOT NULL,
        payload         TEXT    NOT NULL,
        qos             INTEGER NOT NULL DEFAULT 2,
        session_number  TEXT    NOT NULL,
        created_at      TEXT    NOT NULL,
        retry_count     INTEGER NOT NULL DEFAULT 0,
        status          TEXT    NOT NULL DEFAULT 'pending'
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_session_topic
        ON pending_messages (session_number, topic);

    CREATE TABLE IF NOT EXISTS message_history (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        original_id     INTEGER NOT NULL,
        topic           TEXT    NOT NULL,
        payload         TEXT    NOT NULL,
        qos             INTEGER NOT NULL,
        session_number  TEXT    NOT NULL,
        created_at      TEXT    NOT NULL,
        sent_at         TEXT    NOT NULL,
        retry_count     INTEGER NOT NULL DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_history_session
        ON message_history (session_number);

    CREATE INDEX IF NOT EXISTS idx_history_sent_at
        ON message_history (sent_at);
    """

    def __init__(self, db_path=None):
        self.db_path = db_path or config.MESSAGE_STORE_DB
        self.lock = threading.Lock()

        directory = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(directory, exist_ok=True)

        self._init_db()

    # ------------------------------------------------------------------ #
    #  Database helpers                                                    #
    # ------------------------------------------------------------------ #

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        with self.lock:
            conn = self._connect()
            try:
                conn.executescript(self._SCHEMA)
                conn.commit()
                log.info(
                    "Message store initialised: %s",
                    os.path.abspath(self.db_path),
                )
            finally:
                conn.close()

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def enqueue(self, topic, payload, session_number, qos=2):
        """
        Persist a message.  Returns the row ``id`` on success or
        ``None`` if a row with the same ``session_number`` + ``topic``
        already exists and has already been sent (deduplication guard).
        """
        payload_json = json.dumps(payload, separators=(",", ":"))
        created_at = datetime.now().isoformat(timespec="milliseconds")

        with self.lock:
            conn = self._connect()
            try:
                # Check history first — if already sent, suppress duplicate.
                hist = conn.execute(
                    "SELECT id FROM message_history "
                    "WHERE session_number = ? AND topic = ?",
                    (session_number, topic),
                ).fetchone()

                if hist:
                    log.warning(
                        "Duplicate suppressed: session=%s topic=%s "
                        "(already in history)",
                        session_number,
                        topic,
                    )
                    return None

                # Check for an existing pending row with the same key.
                row = conn.execute(
                    "SELECT id, status FROM pending_messages "
                    "WHERE session_number = ? AND topic = ?",
                    (session_number, topic),
                ).fetchone()

                if row:
                    if row["status"] == "sent":
                        log.warning(
                            "Duplicate suppressed: session=%s topic=%s "
                            "(already sent)",
                            session_number,
                            topic,
                        )
                        return None

                    # Row exists but is still pending — update payload
                    # in case it changed (unlikely but safe).
                    conn.execute(
                        "UPDATE pending_messages SET payload = ? "
                        "WHERE id = ?",
                        (payload_json, row["id"]),
                    )
                    conn.commit()
                    log.debug(
                        "Updated existing pending message id=%d", row["id"]
                    )
                    return row["id"]

                cur = conn.execute(
                    "INSERT INTO pending_messages "
                    "(topic, payload, qos, session_number, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (topic, payload_json, qos, session_number, created_at),
                )
                conn.commit()
                row_id = cur.lastrowid
                log.debug(
                    "Enqueued message id=%d session=%s topic=%s",
                    row_id,
                    session_number,
                    topic,
                )
                return row_id
            finally:
                conn.close()

    def mark_sent(self, row_id):
        """Mark a message as successfully published."""
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE pending_messages SET status = 'sent' "
                    "WHERE id = ?",
                    (row_id,),
                )
                conn.commit()
            finally:
                conn.close()

    def delete(self, row_id):
        """Remove a message from the store."""
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM pending_messages WHERE id = ?",
                    (row_id,),
                )
                conn.commit()
            finally:
                conn.close()

    def mark_sent_and_delete(self, row_id):
        """
        Move a pending message to ``message_history`` for audit trail,
        then delete it from ``pending_messages``.
        """
        sent_at = datetime.now().isoformat(timespec="milliseconds")

        with self.lock:
            conn = self._connect()
            try:
                # Fetch the row before moving.
                row = conn.execute(
                    "SELECT topic, payload, qos, session_number, "
                    "       created_at, retry_count "
                    "FROM pending_messages WHERE id = ?",
                    (row_id,),
                ).fetchone()

                if row:
                    conn.execute(
                        "INSERT INTO message_history "
                        "(original_id, topic, payload, qos, "
                        " session_number, created_at, sent_at, "
                        " retry_count) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            row_id,
                            row["topic"],
                            row["payload"],
                            row["qos"],
                            row["session_number"],
                            row["created_at"],
                            sent_at,
                            row["retry_count"],
                        ),
                    )

                conn.execute(
                    "DELETE FROM pending_messages WHERE id = ?",
                    (row_id,),
                )
                conn.commit()
                log.debug(
                    "Message id=%d moved to history (sent_at=%s)",
                    row_id,
                    sent_at,
                )
            finally:
                conn.close()

    def get_pending(self, max_retries=None):
        """
        Return all rows with ``status = 'pending'``.

        If *max_retries* is given, rows whose ``retry_count`` exceeds
        it are skipped (and marked ``failed``).
        """
        if max_retries is None:
            max_retries = config.MESSAGE_MAX_RETRIES

        with self.lock:
            conn = self._connect()
            try:
                # Mark over-retried rows as failed first.
                conn.execute(
                    "UPDATE pending_messages SET status = 'failed' "
                    "WHERE status = 'pending' AND retry_count >= ?",
                    (max_retries,),
                )
                conn.commit()

                rows = conn.execute(
                    "SELECT id, topic, payload, qos, session_number, "
                    "       retry_count "
                    "FROM pending_messages "
                    "WHERE status = 'pending' "
                    "ORDER BY id ASC",
                ).fetchall()

                return [dict(r) for r in rows]
            finally:
                conn.close()

    def increment_retry(self, row_id):
        """Bump the retry counter for a pending message."""
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE pending_messages "
                    "SET retry_count = retry_count + 1 "
                    "WHERE id = ?",
                    (row_id,),
                )
                conn.commit()
            finally:
                conn.close()

    def pending_count(self):
        """Return the number of pending messages."""
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM pending_messages "
                    "WHERE status = 'pending'",
                ).fetchone()
                return row["cnt"] if row else 0
            finally:
                conn.close()

    def cleanup_sent(self):
        """Delete all rows that are already marked 'sent'."""
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM pending_messages WHERE status = 'sent'"
                )
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------ #
    #  History / Audit Trail                                               #
    # ------------------------------------------------------------------ #

    def get_history(self, limit=100, session_number=None):
        """
        Query the audit trail.

        Returns up to *limit* most recent history rows, optionally
        filtered by *session_number*.
        """
        with self.lock:
            conn = self._connect()
            try:
                if session_number:
                    rows = conn.execute(
                        "SELECT id, original_id, topic, payload, qos, "
                        "       session_number, created_at, sent_at, "
                        "       retry_count "
                        "FROM message_history "
                        "WHERE session_number = ? "
                        "ORDER BY sent_at DESC "
                        "LIMIT ?",
                        (session_number, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, original_id, topic, payload, qos, "
                        "       session_number, created_at, sent_at, "
                        "       retry_count "
                        "FROM message_history "
                        "ORDER BY sent_at DESC "
                        "LIMIT ?",
                        (limit,),
                    ).fetchall()

                return [dict(r) for r in rows]
            finally:
                conn.close()

    def history_count(self):
        """Return the total number of history rows."""
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM message_history",
                ).fetchone()
                return row["cnt"] if row else 0
            finally:
                conn.close()
