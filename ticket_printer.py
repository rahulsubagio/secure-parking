import logging
import os
from datetime import datetime

from escpos.printer import Usb

import config

log = logging.getLogger(__name__)


class TicketPrinter:
    def __init__(self):
        self.printer = None

    def connect(self):
        self.printer = Usb(
            config.PRINTER_VID,
            config.PRINTER_PID,
            config.PRINTER_INTERFACE,
            config.PRINTER_IN_EP,
            config.PRINTER_OUT_EP,
        )
        return True

    def _check_paper_status(self):
        """Best-effort ESC/POS paper/status check.

        Not every XP-Q200H firmware/USB interface exposes real-time status.
        If the status command is unsupported, printing is allowed to proceed
        and any actual print exception is handled by main.py as a help event.
        """
        if self.printer is None:
            return True

        try:
            device = getattr(self.printer, "device", None)
            in_ep = getattr(self.printer, "in_ep", config.PRINTER_IN_EP)
            if device is None or not hasattr(device, "read"):
                return True

            # DLE EOT 4 = paper sensor/status in common ESC/POS implementations.
            self.printer._raw(b"\x10\x04\x04")
            data = device.read(in_ep, 1, timeout=300)
            if not data:
                return True

            status = int(data[0])
            # For paper sensor status, bit 2 is commonly the paper-end flag.
            # Firmware varies, so only treat the documented paper-end bit as fault.
            paper_end = bool(status & 0x04)
            if paper_end:
                raise RuntimeError("Printer reports paper end")
        except RuntimeError:
            raise
        except Exception as exc:
            # Unsupported status read must not block a healthy printer.
            log.debug("Printer status query unavailable: %s", exc)

        return True

    def print_ticket(self, session_number, vehicle_type, entry_time):
        if self.printer is None:
            self.connect()

        self._check_paper_status()

        vehicle_label = "Mobil" if vehicle_type == "car" else "Motor"

        p = self.printer

        # Logo is optional. If it does not exist, printing continues.
        if os.path.exists(config.PRINTER_LOGO):
            try:
                p.set(align="center")
                p.image(config.PRINTER_LOGO, impl="bitImageRaster")
            except Exception:
                log.exception("Failed printing logo; continuing without logo.")

        p.set(align="center", bold=True, width=2, height=2)
        p.text(config.HOSPITAL_NAME + "\n")

        p.set(align="center", bold=False, width=1, height=1)
        p.text(config.HOSPITAL_ADDRESS + "\n")
        p.text("--------------------------------\n")

        p.text(f"Waktu Masuk: {entry_time}\n")
        p.text(f"Tipe Kendaraan: {vehicle_label}\n")

        p.set(bold=True)
        p.text(f"ID: {session_number}\n")
        p.set(bold=False)

        # QR content = session number, per project requirement.
        p.set(align="center")
        p.qr(session_number, size=8)

        p.text("\nHARAP TIDAK MENINGGALKAN TIKET DI\n")
        p.text("KENDARAAN. JIKA TIKET HILANG,\n")
        p.text(
            f"{vehicle_label.upper()} DENDA "
            f"{config.TICKET_FINE_CAR if vehicle_type == 'car' else config.TICKET_FINE_MOTOR}\n"
        )
        p.text("KUNCI KENDARAAN ANDA. SEGALA KEHILANGAN\n")
        p.text("BUKAN TANGGUNG JAWAB PENGELOLA\n")
        p.text("TERIMA KASIH\n\n")
        p.cut()

        log.info("Ticket printed: %s", session_number)
