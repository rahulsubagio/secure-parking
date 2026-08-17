import json
import logging
import threading

import paho.mqtt.client as mqtt

import config

log = logging.getLogger(__name__)


class MQTTService:
    """
    MQTT service for Gate IN.

    Member verification intentionally does NOT generate a separate UUID.
    The session number is the transaction/request identifier and is used
    to correlate the verification response.
    """

    def __init__(self):
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=config.MQTT_CLIENT_ID,
        )

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

    def connect(self):
        self.client.connect(
            config.MQTT_HOST,
            config.MQTT_PORT,
            config.MQTT_KEEPALIVE,
        )
        self.client.loop_start()

        if not self.connected_event.wait(5):
            raise TimeoutError("MQTT connection timeout")

    def disconnect(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info(
                "MQTT connected to %s:%s",
                config.MQTT_HOST,
                config.MQTT_PORT,
            )
            self.connected_event.set()
            client.subscribe(config.MQTT_MEMBER_RESPONSE_TOPIC, qos=1)
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

    def _publish(self, topic, payload):
        if not self.connected_event.is_set():
            raise RuntimeError("MQTT is not connected")

        info = self.client.publish(
            topic,
            json.dumps(payload, separators=(",", ":")),
            qos=1,
        )
        info.wait_for_publish()

        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish failed: rc={info.rc}")

    def publish_general(self, session_number, vehicle_type, entry_time):
        payload = {
            "sessionNumber": session_number,
            "vehicleType": vehicle_type,
            "entryTime": entry_time,
        }
        self._publish(config.MQTT_GENERAL_TOPIC, payload)
        log.info("General visitor published: %s", payload)

    def verify_member(self, session_number, member_code, entry_time):
        """
        Publish member verification request.

        There is NO separately generated requestId.
        sessionNumber itself is the unique request/transaction identifier.
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
            self._publish(config.MQTT_MEMBER_REQUEST_TOPIC, payload)

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
        payload = {
            "time": timestamp,
            "reason": reason,
        }
        self._publish(config.MQTT_HELP_TOPIC, payload)
        log.info("Help request published: %s", payload)
