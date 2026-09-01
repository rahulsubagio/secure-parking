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
    Antrean pesan SQLite persisten dengan jejak audit (audit trail).

    Pesan MQTT transaksional (pengunjung umum, request member) 
    disimpan lokal sebelum dikirim. Jika berhasil dikirim, baris
    dipindahkan ke ``message_history``. Jika gagal, baris tetap di 
    ``pending_messages`` agar dicoba lagi oleh worker background.

    Topik ``help`` (bantuan) dikecualikan
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
        Menyimpan pesan. Mengembalikan ``id`` baris jika berhasil, atau 
        ``None`` jika pesan duplikat sudah pernah dikirim (mencegah duplikasi).
        """
        payload_json = json.dumps(payload, separators=(",", ":"))
        created_at = datetime.now().isoformat(timespec="milliseconds")

        with self.lock:
            conn = self._connect()
            try:
                # Cek history dulu — jika sudah terkirim, abaikan duplikat.
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

                # Cek baris pending dengan key yang sama.
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

                    # Baris masih berstatus pending — perbarui payload 
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
        """Tandai pesan sukses terkirim."""
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
        """Hapus pesan dari database."""
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
        Memindahkan pesan pending ke ``message_history`` untuk jejak audit,
        lalu menghapusnya dari ``pending_messages``.
        """
        sent_at = datetime.now().isoformat(timespec="milliseconds")

        with self.lock:
            conn = self._connect()
            try:
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
        Mengembalikan semua baris dengan status ``'pending'``.

        Jika *max_retries* diberikan, baris yang melampaui batas
        percobaan akan dilewati (dan ditandai ``'failed'``).
        """
        if max_retries is None:
            max_retries = config.MESSAGE_MAX_RETRIES

        with self.lock:
            conn = self._connect()
            try:
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
        """Menambah jumlah percobaan (retry) untuk pesan pending."""
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
        """Mengembalikan jumlah pesan yang masih pending."""
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
        """Menghapus semua baris yang sudah berstatus 'sent'."""
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
        Mencari riwayat (audit trail).

        Mengembalikan data sebanyak *limit* baris terbaru, bisa 
        difilter berdasarkan *session_number*.
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
        """Mengembalikan total jumlah baris riwayat (history)."""
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM message_history",
                ).fetchone()
                return row["cnt"] if row else 0
            finally:
                conn.close()
