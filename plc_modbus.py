import logging
import threading
import time

from pymodbus.client import ModbusSerialClient

import config

log = logging.getLogger(__name__)


class PLCModbus:
    """
    BBB = Modbus RTU master
    Outseal Mega V3 = Modbus RTU slave, ID 1.

    Important:
    pymodbus addresses are zero-based.
    """

    def __init__(self):
        self.client = ModbusSerialClient(
            port=config.PLC_PORT,
            baudrate=config.PLC_BAUDRATE,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=config.PLC_TIMEOUT,
        )
        self.lock = threading.Lock()
        self.connected = False

    def _call(self, method, **kwargs):
        """
        Supports pymodbus releases that use either 'device_id' or 'slave'.
        """
        try:
            return method(**kwargs, device_id=config.PLC_SLAVE_ID)
        except TypeError:
            return method(**kwargs, slave=config.PLC_SLAVE_ID)

    def connect(self):
        with self.lock:
            if self.connected:
                return True
            try:
                self.connected = bool(self.client.connect())
                if self.connected:
                    log.info(
                        "PLC connected: %s @ %d baud, slave=%d",
                        config.PLC_PORT,
                        config.PLC_BAUDRATE,
                        config.PLC_SLAVE_ID,
                    )
                else:
                    log.error("PLC connection failed")
            except Exception:
                self.connected = False
                log.exception("PLC connection error")
            return self.connected

    def close(self):
        with self.lock:
            try:
                self.client.close()
            finally:
                self.connected = False

    def _ensure(self):
        if not self.connected:
            return self.connect()
        return True

    def read_inputs(self):
        """Read S.1 ... S.6 as discrete inputs."""
        with self.lock:
            if not self._ensure():
                return None
            try:
                rr = self._call(
                    self.client.read_discrete_inputs,
                    address=0,
                    count=6,
                )
                if rr.isError():
                    raise RuntimeError(f"Modbus read inputs error: {rr}")
                return {
                    "S1": bool(rr.bits[config.PLC["S1"]]),
                    "S2": bool(rr.bits[config.PLC["S2"]]),
                    "S3": bool(rr.bits[config.PLC["S3"]]),
                    "S4": bool(rr.bits[config.PLC["S4"]]),
                    "S5": bool(rr.bits[config.PLC["S5"]]),
                    "S6": bool(rr.bits[config.PLC["S6"]]),
                }
            except Exception:
                self.connected = False
                log.exception("Failed reading PLC inputs")
                return None

    def read_outputs_and_bits(self):
        """
        Read R.1, R.2 and selected B bits.
        Outseal maps R.1..R.128 to coil addresses 0..127,
        and B.1..B.128 to 128..255.
        """
        with self.lock:
            if not self._ensure():
                return None
            try:
                rr = self._call(
                    self.client.read_coils,
                    address=0,
                    count=165,
                )
                if rr.isError():
                    raise RuntimeError(f"Modbus read coils error: {rr}")

                return {
                    "R1": bool(rr.bits[config.PLC["R1"]]),
                    "R2": bool(rr.bits[config.PLC["R2"]]),
                    "B1": bool(rr.bits[config.PLC["B1"]]),
                    "B21": bool(rr.bits[config.PLC["B21"]]),
                    "B37": bool(rr.bits[config.PLC["B37"]]),
                }
            except Exception:
                self.connected = False
                log.exception("Failed reading PLC coils")
                return None

    def open_request(self):
        """
        Pulse B.21. Rung 9 latches B.3 when B.21 is ON.
        Rung 33 in the uploaded ladder then unlatches B.21.
        """
        with self.lock:
            if not self._ensure():
                return False
            try:
                wr = self._call(
                    self.client.write_coil,
                    address=config.PLC["B21"],
                    value=True,
                )
                if wr.isError():
                    raise RuntimeError(f"Modbus write B21 error: {wr}")
                log.info("OPEN_REQUEST sent: B.21 = ON")
                return True
            except Exception:
                self.connected = False
                log.exception("Failed to send OPEN_REQUEST")
                return False

    def wait_gate_open(self, timeout=5.0):
        """Wait until R.1 is observed ON."""
        log.debug(
            "Waiting for R1 (barrier open), timeout=%.1fs", timeout
        )
        deadline = time.monotonic() + timeout
        poll_count = 0
        while time.monotonic() < deadline:
            state = self.read_outputs_and_bits()
            poll_count += 1
            if state and state["R1"]:
                log.info(
                    "R1 = ON detected after %d poll(s) (%.2fs)",
                    poll_count,
                    timeout - (deadline - time.monotonic()),
                )
                return True
            time.sleep(0.05)
        log.warning(
            "R1 not detected after %d poll(s) (%.1fs timeout)",
            poll_count,
            timeout,
        )
        return False

    def snapshot(self):
        inputs = self.read_inputs()
        outputs = self.read_outputs_and_bits()
        if inputs is None or outputs is None:
            return None
        return {**inputs, **outputs}
