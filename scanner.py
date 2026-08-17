import logging
import threading

import evdev

import config

log = logging.getLogger(__name__)

KEYS = {
    "KEY_A": "a", "KEY_B": "b", "KEY_C": "c", "KEY_D": "d",
    "KEY_E": "e", "KEY_F": "f", "KEY_G": "g", "KEY_H": "h",
    "KEY_I": "i", "KEY_J": "j", "KEY_K": "k", "KEY_L": "l",
    "KEY_M": "m", "KEY_N": "n", "KEY_O": "o", "KEY_P": "p",
    "KEY_Q": "q", "KEY_R": "r", "KEY_S": "s", "KEY_T": "t",
    "KEY_U": "u", "KEY_V": "v", "KEY_W": "w", "KEY_X": "x",
    "KEY_Y": "y", "KEY_Z": "z",
    "KEY_0": "0", "KEY_1": "1", "KEY_2": "2", "KEY_3": "3",
    "KEY_4": "4", "KEY_5": "5", "KEY_6": "6", "KEY_7": "7",
    "KEY_8": "8", "KEY_9": "9",
    "KEY_SPACE": " ",
    "KEY_MINUS": "-",
    "KEY_EQUAL": "=",
    "KEY_DOT": ".",
    "KEY_SLASH": "/",
    "KEY_BACKSLASH": "\\",
    "KEY_SEMICOLON": ";",
    "KEY_APOSTROPHE": "'",
}

SHIFTED = {
    "1": "!", "2": "@", "3": "#", "4": "$", "5": "%",
    "6": "^", "7": "&", "8": "*", "9": "(", "0": ")",
    "-": "_", "=": "+", ".": ">", "/": "?", "\\": "|",
    ";": ":", "'": '"',
}


class QRScanner:
    def __init__(self, callback):
        self.callback = callback
        self.device = None
        self.thread = None
        self.stop_event = threading.Event()

    def start(self):
        self.thread = threading.Thread(
            target=self._run,
            name="qr-scanner",
            daemon=True,
        )
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        try:
            if self.device:
                self.device.ungrab()
        except Exception:
            pass

    def _run(self):
        try:
            self.device = evdev.InputDevice(config.SCANNER_DEVICE)
            try:
                self.device.grab()
            except Exception:
                log.warning("Could not grab scanner; continuing anyway.")

            log.info(
                "QR scanner ready: %s (%s)",
                self.device.path,
                self.device.name,
            )

            buffer = []
            shift = False

            for event in self.device.read_loop():
                if self.stop_event.is_set():
                    break

                if event.type != evdev.ecodes.EV_KEY:
                    continue

                key_event = evdev.categorize(event)

                if key_event.keycode in (
                    "KEY_LEFTSHIFT",
                    "KEY_RIGHTSHIFT",
                ):
                    shift = key_event.keystate != key_event.key_up
                    continue

                if key_event.keystate != key_event.key_down:
                    continue

                key = key_event.keycode
                if isinstance(key, list):
                    key = key[0]

                if key == "KEY_ENTER":
                    data = "".join(buffer).strip()
                    buffer.clear()
                    if data:
                        log.info("QR data received: %s", data)
                        self.callback(data)
                    continue

                char = KEYS.get(key)
                if char is None:
                    continue

                if shift:
                    if char.isalpha():
                        char = char.upper()
                    else:
                        char = SHIFTED.get(char, char)

                buffer.append(char)

        except FileNotFoundError:
            log.error("QR scanner not found: %s", config.SCANNER_DEVICE)
        except PermissionError:
            log.error(
                "Permission denied for %s. Add BBB user to input group.",
                config.SCANNER_DEVICE,
            )
        except Exception:
            log.exception("QR scanner stopped unexpectedly")
        finally:
            try:
                if self.device:
                    self.device.ungrab()
            except Exception:
                pass
