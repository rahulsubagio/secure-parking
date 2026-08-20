import json
import logging
import threading
import time
import ssl

import paho.mqtt.client as mqtt

import config
from message_store import MessageStore

log = logging.getLogger(__name__)


class MQTTService:
    """
    MQTT service for Gate IN.

    Publishing strategy:
    - **Transactional** topics (general, member/request) use QoS 2
      (exactly-once) and are persisted in a local SQLite store before
      being published.  A background retry worker re-publishes any
      messages that failed due to connectivity issues.
    - **Realtime** topics (help) use QoS 1 and are NOT stored locally.
      If the publish fails, a warning is logged but the message is not
      retried — help events must be delivered immediately or not at all.

    Member verification intentionally does NOT generate a separate UUID.
    The session number is the transaction/request identifier and is used
    to correlate the verification response.
    """

    def __init__(self):
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=config.MQTT_CLIENT_ID,
            clean_session=False,
        )

        self.client.tls_set(ca_certs=None, cert_reqs=ssl.CERT_REQUIRED)

        if config.MQTT_USERNAME:
            self.client.username_pw_set(
                config.MQTT_USERNAME,
                config.MQTT_PASSWORD,
            )

        self.connected_event = threading.Event()
        self.member_lock = threading.Lock()

        # session_number -> {"event": Event, "payload": dict|None}
        self.member_results = {}

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        # Local message store for transactional topics.
        self.store = MessageStore()

        # Retry worker control.
        self._retry_stop = threading.Event()
        self._retry_thread = None

    def connect(self):
        """
        Attempt to connect to the MQTT broker.

        If the broker is unreachable, the application continues in
        **offline mode**.  The background retry worker will
        periodically attempt to reconnect.  Once the broker becomes
        available, the connection is established automatically and
        any queued messages are delivered.
        """
        # Configure paho auto-reconnect for *post-disconnect* scenarios
        # (exponential back-off from 1s to 30s).
        self.client.reconnect_delay_set(
            min_delay=1,
            max_delay=30,
        )

        try:
            self.client.connect(
                config.MQTT_HOST,
                config.MQTT_PORT,
                config.MQTT_KEEPALIVE,
            )
            self.client.loop_start()

            if self.connected_event.wait(5):
                log.info(
                    "MQTT connected to %s:%s",
                    config.MQTT_HOST,
                    config.MQTT_PORT,
                )
            else:
                log.warning(
                    "MQTT connection to %s:%s timed out — "
                    "starting in OFFLINE mode",
                    config.MQTT_HOST,
                    config.MQTT_PORT,
                )

        except Exception as exc:
            log.warning(
                "MQTT broker unreachable (%s:%s): %s — "
                "starting in OFFLINE mode",
                config.MQTT_HOST,
                config.MQTT_PORT,
                exc,
            )
            # Start the network loop anyway — it will be idle until we
            # successfully call reconnect() from the retry worker.
            try:
                self.client.loop_start()
            except Exception:
                pass

        # Always start the retry worker, regardless of connection status.
        self._start_retry_worker()

    def disconnect(self):
        self._retry_stop.set()
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
        if self._retry_thread and self._retry_thread.is_alive():
            self._retry_thread.join(timeout=3)

    # ------------------------------------------------------------------ #
    #  Callbacks                                                           #
    # ------------------------------------------------------------------ #

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info(
                "[MQTT] === KONEKSI MQTT BERHASIL === "
                "Terhubung ke %s:%s",
                config.MQTT_HOST,
                config.MQTT_PORT,
            )
            self.connected_event.set()
            client.subscribe(config.MQTT_MEMBER_RESPONSE_TOPIC, qos=1)

            # Trigger immediate retry of any queued messages.
            pending = self.store.pending_count()
            if pending > 0:
                log.info(
                    "MQTT reconnected — %d pending message(s) queued "
                    "for retry",
                    pending,
                )
        else:
            log.error("MQTT connection rejected: %s", reason_code)

    def _on_disconnect(
        self, client, userdata, disconnect_flags, reason_code, properties
    ):
        self.connected_event.clear()
        log.warning("MQTT disconnected: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            log.warning("Invalid JSON on %s", msg.topic)
            return

        # Primary correlation key: sessionNumber.
        # For compatibility, requestId is accepted only if a server sends
        # it back, but BBB does not generate or publish a separate requestId.
        session_number = str(payload.get("sessionNumber", "")).strip()

        if not session_number:
            request_id = str(payload.get("requestId", "")).strip()
            if request_id:
                session_number = request_id

        if not session_number:
            log.warning(
                "Member response ignored: no sessionNumber on %s",
                msg.topic,
            )
            return

        with self.member_lock:
            result = self.member_results.get(session_number)
            if result:
                result["payload"] = payload
                result["event"].set()
                log.info(
                    "Member verification response matched session=%s",
                    session_number,
                )

    # ------------------------------------------------------------------ #
    #  Publish: persistent (QoS 2 + SQLite)                                #
    # ------------------------------------------------------------------ #

    def _publish_persistent(self, topic, payload, session_number):
        """
        Store the message locally, then attempt to publish with QoS 2.

        If the publish succeeds the row is deleted from the store.
        If it fails (disconnected, timeout, etc.) the message remains
        in SQLite and will be retried by the background worker.

        Returns ``True`` if the message was published immediately,
        ``False`` if it was queued for later delivery.
        """
        row_id = self.store.enqueue(topic, payload, session_number, qos=2)

        if row_id is None:
            # Deduplication: message was already sent.
            log.info(
                "Message already sent — skipping: session=%s topic=%s",
                session_number,
                topic,
            )
            return True

        if not self.connected_event.is_set():
            log.warning(
                "MQTT offline — message queued locally: session=%s topic=%s",
                session_number,
                topic,
            )
            return False

        try:
            info = self.client.publish(
                topic,
                json.dumps(payload, separators=(",", ":")),
                qos=2,
            )
            info.wait_for_publish(timeout=10)

            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError(f"MQTT publish failed: rc={info.rc}")

            # Publish succeeded — remove from local store.
            self.store.mark_sent_and_delete(row_id)
            log.info(
                "Published (QoS 2) and cleared from store: "
                "session=%s topic=%s",
                session_number,
                topic,
            )
            return True

        except Exception as exc:
            log.warning(
                "Publish failed (will retry): session=%s topic=%s — %s",
                session_number,
                topic,
                exc,
            )
            self.store.increment_retry(row_id)
            return False

    # ------------------------------------------------------------------ #
    #  Publish: realtime (QoS 1, no store)                                 #
    # ------------------------------------------------------------------ #

    def _publish_realtime(self, topic, payload):
        """
        Fire-and-forget publish at QoS 1.

        Used for the ``help`` topic which must be delivered in real-time.
        Messages are NOT stored in the local queue and are NOT retried.
        """
        if not self.connected_event.is_set():
            log.warning(
                "MQTT offline — realtime message dropped: topic=%s",
                topic,
            )
            return False

        try:
            info = self.client.publish(
                topic,
                json.dumps(payload, separators=(",", ":")),
                qos=1,
            )
            info.wait_for_publish(timeout=5)

            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError(f"MQTT publish failed: rc={info.rc}")

            return True

        except Exception as exc:
            log.warning(
                "Realtime publish failed (not retried): topic=%s — %s",
                topic,
                exc,
            )
            return False

    # ------------------------------------------------------------------ #
    #  Background retry worker + auto-reconnect                            #
    # ------------------------------------------------------------------ #

    def _start_retry_worker(self):
        if self._retry_thread and self._retry_thread.is_alive():
            return
        self._retry_stop.clear()
        self._retry_thread = threading.Thread(
            target=self._retry_loop,
            name="mqtt-retry-worker",
            daemon=True,
        )
        self._retry_thread.start()
        log.info("MQTT retry/reconnect worker started")

    def _try_reconnect(self):
        """
        Attempt to (re)connect to the MQTT broker.

        Called by the retry worker when the connection is down.
        Returns ``True`` if the connection was established.
        """
        try:
            self.client.reconnect()
            # Give paho a moment to complete the handshake.
            if self.connected_event.wait(5):
                log.info(
                    "[MQTT] === KONEKSI MQTT BERHASIL DIPULIHKAN === "
                    "Terhubung kembali ke %s:%s",
                    config.MQTT_HOST,
                    config.MQTT_PORT,
                )
                return True
            log.debug("MQTT reconnect handshake timed out")
            return False
        except Exception as exc:
            log.debug("MQTT reconnect failed: %s", exc)
            return False

    def _retry_loop(self):
        """
        Periodically:
        1. If disconnected — attempt to reconnect.
        2. If connected — re-publish any pending messages.
        """
        while not self._retry_stop.is_set():
            self._retry_stop.wait(config.MESSAGE_RETRY_INTERVAL)

            if self._retry_stop.is_set():
                break

            # --- Auto-reconnect ---
            if not self.connected_event.is_set():
                pending = self.store.pending_count()
                log.info(
                    "MQTT offline — attempting reconnect... "
                    "(%d pending message(s) queued)",
                    pending,
                )
                self._try_reconnect()
                # Whether reconnect succeeded or not, wait for next cycle.
                continue

            # --- Retry pending messages ---
            pending = self.store.get_pending()
            if not pending:
                continue

            log.info(
                "[MQTT-SYNC] Memulai sinkronisasi %d transaksi offline "
                "yang tertunda...",
                len(pending),
            )

            for msg in pending:
                if self._retry_stop.is_set():
                    break
                if not self.connected_event.is_set():
                    log.warning(
                        "Connection lost during retry — "
                        "will resume next cycle"
                    )
                    break

                try:
                    payload_json = msg["payload"]
                    info = self.client.publish(
                        msg["topic"],
                        payload_json,
                        qos=msg["qos"],
                    )
                    info.wait_for_publish(timeout=10)

                    if info.rc != mqtt.MQTT_ERR_SUCCESS:
                        raise RuntimeError(
                            f"MQTT publish failed: rc={info.rc}"
                        )

                    self.store.mark_sent_and_delete(msg["id"])
                    log.info(
                        "[MQTT-SYNC] TRANSAKSI OFFLINE TERKIRIM: "
                        "session=%s | topic=%s | id=%d",
                        msg["session_number"],
                        msg["topic"],
                        msg["id"],
                    )

                except Exception as exc:
                    self.store.increment_retry(msg["id"])
                    log.warning(
                        "Retry FAILED: id=%d session=%s — %s",
                        msg["id"],
                        msg["session_number"],
                        exc,
                    )

        log.info("MQTT retry/reconnect worker stopped")

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def publish_general(self, session_number, vehicle_type, entry_time):
        """
        Publish general visitor entry.  QoS 2 + local store.

        Returns ``True`` if published immediately, ``False`` if queued.
        """
        payload = {
            "sessionNumber": session_number,
            "vehicleType": vehicle_type,
            "entryTime": entry_time,
        }
        sent = self._publish_persistent(
            config.MQTT_GENERAL_TOPIC, payload, session_number
        )
        log.info(
            "General visitor %s: %s",
            "published" if sent else "queued",
            payload,
        )
        return sent

    def verify_member(self, session_number, member_code, entry_time):
        """
        Publish member verification request.  QoS 2 + local store.

        There is NO separately generated requestId.
        sessionNumber itself is the unique request/transaction identifier.

        The request is always stored locally.  However, the *response*
        must arrive in real-time for the gate to open; if the broker is
        unreachable the verification will time out regardless.
        """
        payload = {
            "sessionNumber": session_number,
            "memberCode": member_code,
            "entryTime": entry_time,
        }

        event = threading.Event()
        with self.member_lock:
            self.member_results[session_number] = {
                "event": event,
                "payload": None,
            }

        try:
            sent = self._publish_persistent(
                config.MQTT_MEMBER_REQUEST_TOPIC, payload, session_number
            )

            if not sent:
                raise RuntimeError(
                    "MQTT offline — member verification request queued "
                    "but cannot wait for response"
                )

            if not event.wait(config.MEMBER_VERIFY_TIMEOUT):
                raise TimeoutError(
                    f"Member verification timeout "
                    f"({config.MEMBER_VERIFY_TIMEOUT}s), "
                    f"session={session_number}"
                )

            with self.member_lock:
                result = self.member_results[session_number]["payload"]

            if not result:
                raise RuntimeError(
                    f"Empty member verification response, "
                    f"session={session_number}"
                )

            success = bool(result.get("success", False))

            log.info(
                "Member verification result: session=%s memberCode=%s success=%s",
                session_number,
                member_code,
                success,
            )
            return success, result

        finally:
            with self.member_lock:
                self.member_results.pop(session_number, None)

    def publish_help(self, timestamp, reason):
        """
        Publish a help request.  QoS 1, realtime, NO local store.

        If MQTT is offline, the message is dropped with a warning.
        """
        payload = {
            "time": timestamp,
            "reason": reason,
        }
        sent = self._publish_realtime(config.MQTT_HELP_TOPIC, payload)
        if sent:
            log.info("Help request published: %s", payload)
        else:
            log.warning("Help request NOT sent (MQTT offline): %s", payload)
