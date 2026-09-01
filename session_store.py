import os
import threading
from datetime import datetime

import config


class SessionStore:
    """
    Pembuatan nomor urut harian:
    Nomor sesi = YYYYMMDD + 4 digit urutan.
    Contoh: 202608170001
    """

    def __init__(self, path=None):
        self.path = path or config.SESSION_SEQUENCE_FILE
        self.lock = threading.Lock()

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = f.read().strip()
                return int(data) if data else 0
        except FileNotFoundError:
            return 0

    def _write(self, value):
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(value))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def next_session(self):
        with self.lock:
            today = datetime.now().strftime("%Y%m%d")
            seq = self._read()

            # File menyimpan nomor urut yang digunakan hari ini.
            # Akan di-reset pada awal hari yang baru.
            marker_path = self.path + ".date"
            try:
                with open(marker_path, "r", encoding="utf-8") as f:
                    marker = f.read().strip()
            except FileNotFoundError:
                marker = ""

            if marker != today:
                seq = 0

            seq += 1
            if seq > 9999:
                raise RuntimeError("Daily session sequence exceeded 9999")

            self._write(seq)
            with open(marker_path, "w", encoding="utf-8") as f:
                f.write(today)

            return f"{today}{seq:04d}"
