#!/usr/bin/env python3
import logging
import signal
import threading
import time
from datetime import datetime

import config
from mqtt_service import MQTTService
from plc_modbus import PLCModbus
from scanner import QRScanner
from session_store import SessionStore
from ticket_printer import TicketPrinter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("gate-in")


def iso_now():
    # Project format: yyyy-MM-dd'T'HH:mm:ss.SSS
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


class GateInApp:

    # Friendly labels for log output.
    _INPUT_LABELS = {
        "S1": "Tombol Hijau",
        "S2": "Tombol Kuning",
        "S3": "Tombol Merah",
        "S4": "VLD Dispenser",
        "S5": "VLD Barrier",
        "S6": "Photocell",
    }
    _OUTPUT_LABELS = {
        "R1": "Barrier OPEN",
        "R2": "Barrier CLOSE",
        "B1": "State Proses",
        "B21": "BBB Open Req",
        "B37": "Mirror B1",
    }

    def __init__(self):
        self.stop_event = threading.Event()

        self.plc = PLCModbus()
        self.mqtt = MQTTService()
        self.printer = TicketPrinter()
        self.sessions = SessionStore()

        self.scanner = QRScanner(self.on_qr)

        self.state_lock = threading.Lock()
        self.vehicle_present = False
        self.session_active = False
        self.session_started_at = 0.0

        self.last_inputs = None
        self.last_outputs = None

        self.last_s1 = False
        self.last_s2 = False
        self.last_s3 = False
        self.last_s4 = False

        self.last_event_time = {
            "S1": 0.0,
            "S2": 0.0,
            "S3": 0.0,
            "S4": 0.0,
        }

    # ---------------- logging helpers ----------------

    @staticmethod
    def _on_off(val):
        return "ON" if val else "OFF"

    def _format_inputs(self, inputs):
        """Format input states as a single-line summary."""
        parts = []
        for key in ("S1", "S2", "S3", "S4", "S5", "S6"):
            label = self._INPUT_LABELS.get(key, key)
            parts.append(f"{key}({label})={self._on_off(inputs[key])}")
        return " | ".join(parts)

    def _format_outputs(self, outputs):
        """Format output states as a single-line summary."""
        parts = []
        for key in ("R1", "R2", "B1", "B21", "B37"):
            label = self._OUTPUT_LABELS.get(key, key)
            parts.append(f"{key}({label})={self._on_off(outputs[key])}")
        return " | ".join(parts)

    @staticmethod
    def _log_step(step_num, total, description):
        """Log a numbered transaction step."""
        log.info(">>> STEP [%d/%d] %s", step_num, total, description)

    # ---------------- lifecycle ----------------

    def start(self):
        log.info("========================================")
        log.info("  Starting Gate IN BBB application")
        log.info("========================================")

        log.info("[INIT] Connecting to MQTT broker...")
        self.mqtt.connect()
        if self.mqtt.connected_event.is_set():
            log.info("[INIT] MQTT connected OK")
        else:
            log.warning(
                "[INIT] MQTT tidak tersedia — "
                "sistem berjalan dalam mode OFFLINE. "
                "Auto-reconnect aktif."
            )

        log.info("[INIT] Connecting to PLC (Modbus RTU)...")
        if not self.plc.connect():
            raise RuntimeError("PLC Modbus connection failed")
        log.info("[INIT] PLC connected OK")

        log.info("[INIT] Starting QR scanner...")
        self.scanner.start()
        log.info("[INIT] QR scanner started OK")

        log.info("[INIT] All subsystems ready — entering PLC poll loop")
        # Main PLC polling loop.
        self.run()

    def stop(self):
        if self.stop_event.is_set():
            return

        self.stop_event.set()
        self.scanner.stop()
        self.mqtt.disconnect()
        self.plc.close()
        log.info("Gate IN application stopped")

    # ---------------- PLC polling ----------------

    def run(self):
        while not self.stop_event.is_set():
            inputs = self.plc.read_inputs()
            if inputs is None:
                time.sleep(1)
                continue

            outputs = self.plc.read_outputs_and_bits()

            # --- Log input state changes ---
            if self.last_inputs is not None and inputs != self.last_inputs:
                changed = [
                    k for k in inputs
                    if inputs[k] != self.last_inputs.get(k)
                ]
                for k in changed:
                    label = self._INPUT_LABELS.get(k, k)
                    log.info(
                        "[INPUT]  %s (%s): %s -> %s",
                        k, label,
                        self._on_off(self.last_inputs.get(k)),
                        self._on_off(inputs[k]),
                    )
                log.info("[INPUT]  State: %s", self._format_inputs(inputs))

            # --- Log output state changes ---
            if (
                outputs is not None
                and self.last_outputs is not None
                and outputs != self.last_outputs
            ):
                changed = [
                    k for k in outputs
                    if outputs[k] != self.last_outputs.get(k)
                ]
                for k in changed:
                    label = self._OUTPUT_LABELS.get(k, k)
                    log.info(
                        "[OUTPUT] %s (%s): %s -> %s",
                        k, label,
                        self._on_off(self.last_outputs.get(k)),
                        self._on_off(outputs[k]),
                    )
                log.info(
                    "[OUTPUT] State: %s", self._format_outputs(outputs)
                )

            self.last_inputs = inputs
            self.last_outputs = outputs

            self.process_input_edges(inputs)

            # Keep B21 strictly as a trigger generated by BBB.
            # Rung 33 in PLC automatically unlatches it.
            #
            # We do not write R1/R2 from BBB.

            self.check_session_timeout()

            time.sleep(config.PLC_POLL_INTERVAL)

    def process_input_edges(self, x):
        now = time.monotonic()

        s1 = x["S1"]
        s2 = x["S2"]
        s3 = x["S3"]
        s4 = x["S4"]

        # S4 rising edge = vehicle detected at dispenser.
        if s4 and not self.last_s4:
            log.info("[EDGE]   S4 (VLD Dispenser) RISING — kendaraan terdeteksi")
            self.on_vehicle_detected()

        # S1 rising edge = motorcycle.
        if s1 and not self.last_s1:
            log.info("[EDGE]   S1 (Tombol Hijau) RISING — pilihan: MOTOR")
            self.on_vehicle_choice("motorcycle")

        # S2 rising edge = car.
        if s2 and not self.last_s2:
            log.info("[EDGE]   S2 (Tombol Kuning) RISING — pilihan: MOBIL")
            self.on_vehicle_choice("car")

        # S3 is physically NC in your project.
        # The logical value must be verified in Outseal Online Monitor.
        # Here we treat a logical rising edge as "help event".
        if s3 and not self.last_s3:
            log.info("[EDGE]   S3 (Tombol Merah) RISING — BANTUAN diminta")
            self.on_help_request()

        self.last_s1 = s1
        self.last_s2 = s2
        self.last_s3 = s3
        self.last_s4 = s4

    # ---------------- vehicle/session ----------------

    def on_vehicle_detected(self):
        with self.state_lock:
            if self.session_active:
                log.warning(
                    "[SESSION] S4 detected while previous session active "
                    "— IGNORED"
                )
                return

            self.vehicle_present = True
            self.session_active = True
            self.session_started_at = time.monotonic()

        log.info(
            "[SESSION] === SESI BARU === Kendaraan terdeteksi di dispenser"
        )

    def on_vehicle_choice(self, vehicle_type):
        with self.state_lock:
            if not self.session_active:
                log.warning(
                    "[SESSION] Pilihan %s DIABAIKAN — tidak ada sesi aktif",
                    vehicle_type.upper(),
                )
                return

        # Debounce.
        if not self.debounced(vehicle_type):
            return

        log.info(
            "[SESSION] Pilihan kendaraan diterima: %s",
            vehicle_type.upper(),
        )
        threading.Thread(
            target=self.handle_general_visitor,
            args=(vehicle_type,),
            name=f"general-{vehicle_type}",
            daemon=True,
        ).start()

    def on_qr(self, member_code):
        with self.state_lock:
            if not self.session_active:
                log.warning(
                    "[QR] Scan QR DIABAIKAN — tidak ada sesi aktif"
                )
                return

        log.info("[QR] QR code diterima: %s", member_code.strip())
        threading.Thread(
            target=self.handle_member,
            args=(member_code.strip(),),
            name="member-handler",
            daemon=True,
        ).start()

    def handle_general_visitor(self, vehicle_type):
        session_number = self.sessions.next_session()
        entry_time = iso_now()
        vehicle_label = "MOTOR" if vehicle_type == "motorcycle" else "MOBIL"

        log.info("")
        log.info("============================================")
        log.info("  TRANSAKSI PENGUNJUNG UMUM (%s)", vehicle_label)
        log.info("  Session : %s", session_number)
        log.info("  Waktu   : %s", entry_time)
        log.info("============================================")

        try:
            # Step 1: Simpan & publish ke MQTT
            self._log_step(1, 4, "Menyimpan data ke SQLite & publish MQTT...")
            sent = self.mqtt.publish_general(
                session_number,
                vehicle_type,
                entry_time,
            )

            if sent:
                log.info(
                    ">>> STEP [1/4] BERHASIL — data terkirim ke server (QoS 2)"
                )
            else:
                log.warning(
                    ">>> STEP [1/4] MQTT OFFLINE — data disimpan lokal, "
                    "akan dikirim ulang otomatis. Gate tetap buka."
                )

            # Step 2: Cetak tiket
            self._log_step(2, 4, "Mencetak tiket...")
            try:
                self.printer.print_ticket(
                    session_number,
                    vehicle_type,
                    entry_time,
                )
                log.info(">>> STEP [2/4] BERHASIL — tiket tercetak")
            except Exception as exc:
                log.error(
                    ">>> STEP [2/4] GAGAL — printer error: %s", exc
                )
                log.info(
                    ">>> Mengirim permintaan bantuan (printer error)..."
                )
                self.publish_help(config.HELP_REASON_PRINTER)
                log.warning(
                    ">>> TRANSAKSI DIBATALKAN — gate TIDAK dibuka "
                    "(printer error)"
                )
                return

            # Step 3: Kirim B21 ke PLC
            self._log_step(3, 4, "Mengirim OPEN_REQUEST (B21) ke PLC...")
            if not self.plc.open_request():
                log.error(">>> STEP [3/4] GAGAL — tidak bisa menulis B21")
                raise RuntimeError("Could not send B21 OPEN_REQUEST")
            log.info(">>> STEP [3/4] BERHASIL — B21 = ON dikirim ke PLC")

            # Step 4: Tunggu R1 dari PLC
            self._log_step(4, 4, "Menunggu barrier terbuka (R1)...")
            if self.plc.wait_gate_open(timeout=5):
                log.info(">>> STEP [4/4] BERHASIL — R1 ON, barrier TERBUKA")
                log.info("============================================")
                log.info("  TRANSAKSI SELESAI — SUKSES")
                log.info("============================================")
            else:
                log.warning(
                    ">>> STEP [4/4] TIMEOUT — B21 terkirim tapi R1 "
                    "tidak terdeteksi dalam 5 detik"
                )

        except Exception:
            log.exception(
                ">>> TRANSAKSI GAGAL — gate TIDAK dibuka"
            )

        finally:
            self.finish_transaction_state()

    def handle_member(self, member_code):
        if not member_code:
            log.warning("[MEMBER] Kode member kosong — diabaikan")
            return

        session_number = self.sessions.next_session()
        entry_time = iso_now()

        log.info("")
        log.info("============================================")
        log.info("  TRANSAKSI MEMBER")
        log.info("  Session : %s", session_number)
        log.info("  Member  : %s", member_code)
        log.info("  Waktu   : %s", entry_time)
        log.info("============================================")

        try:
            # Step 1: Kirim verifikasi ke server
            self._log_step(1, 3, "Mengirim verifikasi member ke server...")
            verified, response = self.mqtt.verify_member(
                session_number,
                member_code,
                entry_time,
            )

            if not verified:
                log.warning(
                    ">>> STEP [1/3] DITOLAK — member '%s' TIDAK TERVERIFIKASI",
                    member_code,
                )
                log.warning(
                    ">>> TRANSAKSI DIBATALKAN — gate TIDAK dibuka "
                    "(member ditolak)"
                )
                return

            log.info(
                ">>> STEP [1/3] BERHASIL — member '%s' TERVERIFIKASI",
                member_code,
            )

            # Step 2: Kirim B21 ke PLC
            self._log_step(2, 3, "Mengirim OPEN_REQUEST (B21) ke PLC...")
            if not self.plc.open_request():
                log.error(">>> STEP [2/3] GAGAL — tidak bisa menulis B21")
                raise RuntimeError("Could not send B21 OPEN_REQUEST")
            log.info(">>> STEP [2/3] BERHASIL — B21 = ON dikirim ke PLC")

            # Step 3: Tunggu R1 dari PLC
            self._log_step(3, 3, "Menunggu barrier terbuka (R1)...")
            if self.plc.wait_gate_open(timeout=5):
                log.info(">>> STEP [3/3] BERHASIL — R1 ON, barrier TERBUKA")
                log.info("============================================")
                log.info("  TRANSAKSI MEMBER SELESAI — SUKSES")
                log.info("============================================")
            else:
                log.warning(
                    ">>> STEP [3/3] TIMEOUT — B21 terkirim tapi R1 "
                    "tidak terdeteksi dalam 5 detik"
                )

        except TimeoutError:
            log.error(
                ">>> STEP [1/3] TIMEOUT — server tidak merespons dalam "
                "%ds. Gate TIDAK dibuka.",
                config.MEMBER_VERIFY_TIMEOUT,
            )
        except Exception:
            log.exception(
                ">>> TRANSAKSI MEMBER GAGAL — gate TIDAK dibuka"
            )
        finally:
            self.finish_transaction_state()

    def on_help_request(self):
        if not self.debounced("S3"):
            return

        log.info("[HELP] Permintaan bantuan diterima (tombol S3)")
        self.publish_help(config.HELP_REASON_BUTTON)

    def publish_help(self, reason):
        timestamp = iso_now()
        log.info("[HELP] Mengirim help request: %s", reason)
        try:
            self.mqtt.publish_help(timestamp, reason)
        except Exception:
            log.exception("[HELP] GAGAL mengirim help request: %s", reason)


    def finish_transaction_state(self):
        with self.state_lock:
            self.session_active = False
            self.vehicle_present = False
            self.session_started_at = 0.0
        log.info("[SESSION] Sesi direset — siap menerima kendaraan baru")

    def check_session_timeout(self):
        with self.state_lock:
            active = self.session_active
            started = self.session_started_at

        if active and started:
            elapsed = time.monotonic() - started
            if elapsed > config.USER_ACTION_TIMEOUT:
                log.warning(
                    "[SESSION] TIMEOUT — kendaraan tidak memilih dalam "
                    "%.0f detik. Sesi direset.",
                    elapsed,
                )
                self.finish_transaction_state()

    def debounced(self, key):
        now = time.monotonic()
        last = self.last_event_time.get(key, 0.0)

        if now - last < config.EVENT_DEBOUNCE:
            return False

        self.last_event_time[key] = now
        return True


app = GateInApp()


def shutdown_handler(signum, frame):
    log.info("Signal %s received", signum)
    app.stop()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        app.start()
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("Fatal application error")
        raise
    finally:
        app.stop()
