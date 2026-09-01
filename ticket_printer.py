import logging
import os
from datetime import datetime
from PIL import Image

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
        """Pengecekan status kertas printer ESC/POS."""
        if self.printer is None:
            return True

        try:
            device = getattr(self.printer, "device", None)
            in_ep = getattr(self.printer, "in_ep", config.PRINTER_IN_EP)
            if device is None or not hasattr(device, "read"):
                return True

            self.printer._raw(b"\x10\x04\x04")
            data = device.read(in_ep, 1, timeout=300)
            if not data:
                return True

            status = int(data[0])
            paper_end = bool(status & 0x04)
            if paper_end:
                raise RuntimeError("Kertas printer habis")
        except RuntimeError:
            raise
        except Exception as exc:
            log.debug("Gagal membaca status printer: %s", exc)

        return True

    def print_ticket(self, session_number, vehicle_type, entry_time):
        if self.printer is None:
            self.connect()

        self._check_paper_status()

        vehicle_label = "Mobil" if vehicle_type == "car" else "Motor"

        p = self.printer

        p.set(align="center", bold=True, width=2, height=2)
        p.text(config.HOSPITAL_NAME + "\n")

        p.set(align="center", bold=False, width=1, height=1)
        p.text(config.HOSPITAL_ADDRESS + "\n")
        p.text("--------------------------------\n")

        display_time = entry_time.replace("T", " ").split(".")[0]
        
        p.text(f"Waktu Masuk: {display_time}\n")
        p.text(f"Tipe Kendaraan: {vehicle_label}\n")

        p.set(bold=True)
        p.text(f"ID: {session_number}\n")
        p.set(bold=False)

        p.set(align="center")
        p.qr(session_number, size=8)

        p.text("HARAP TIDAK MENINGGALKAN KARCIS DI\n")
        p.text("KENDARAAN. JIKA KARCIS HILANG,\n")
        p.text(
            f"{vehicle_label.upper()} DENDA "
            f"{config.TICKET_FINE_CAR if vehicle_type == 'car' else config.TICKET_FINE_MOTOR}\n"
        )
        p.text("KUNCI KENDARAAN ANDA. SEGALA KEHILANGAN\n")
        p.text("BUKAN TANGGUNG JAWAB PENGELOLA\n")
        p.text("TERIMA KASIH\n\n")
        p.cut()

        log.info("Ticket printed: %s", session_number)
