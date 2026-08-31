import os
from dotenv import load_dotenv

load_dotenv()

# ---------------- PLC / MODBUS ----------------
PLC_PORT = os.getenv("PLC_PORT", "/dev/ttyUSB0")
PLC_BAUDRATE = int(os.getenv("PLC_BAUDRATE", "9600"))
PLC_SLAVE_ID = int(os.getenv("PLC_SLAVE_ID", "1"))
PLC_TIMEOUT = float(os.getenv("PLC_TIMEOUT", "0.5"))
PLC_POLL_INTERVAL = float(os.getenv("PLC_POLL_INTERVAL", "0.10"))

# pymodbus uses ZERO-BASED addresses.
# Outseal slave map:
# R.1 = coil 0, R.2 = coil 1
# B.1 = coil 128, therefore B.21 = 148, B.37 = 164
# S.1 = discrete input 0 ... S.6 = discrete input 5
PLC = {
    "R1": 0,
    "R2": 1,
    "R3": 2,
    "B11": 138,       # Latch Tombol Hijau (Motor)
    "B12": 139,       # Latch Tombol Kuning (Mobil)
    "B13": 140,       # Latch Tombol Merah (Bantuan)
    "B21": 148,       # BBB_OPEN_REQUEST
    "B31": 158,       # Status S.4 VLD Dispenser
    "S1": 0,
    "S2": 1,
    "S3": 2,
    "S4": 3,
    "S5": 4,
    "S6": 5,
}

# ---------------- QR SCANNER ----------------
SCANNER_DEVICE = os.getenv("SCANNER_DEVICE", "/dev/input/event1")

# ---------------- PRINTER ----------------
PRINTER_VID = int(os.getenv("PRINTER_VID", "0x1FC9"), 16)
PRINTER_PID = int(os.getenv("PRINTER_PID", "0x2016"), 16)
PRINTER_IN_EP = int(os.getenv("PRINTER_IN_EP", "0x81"), 16)
PRINTER_OUT_EP = int(os.getenv("PRINTER_OUT_EP", "0x03"), 16)
PRINTER_INTERFACE = int(os.getenv("PRINTER_INTERFACE", "0"))
PRINTER_LOGO = os.getenv("PRINTER_LOGO", "./logo.png")
PRINTER_LOGO_WIDTH = int(os.getenv("PRINTER_LOGO_WIDTH", "380"))

HOSPITAL_NAME = os.getenv("HOSPITAL_NAME", "RUMAH SAKIT RAJAWALI CITRA")
HOSPITAL_ADDRESS = os.getenv("HOSPITAL_ADDRESS", "Jl. Pleret No.KM 2.5")
TICKET_FINE_CAR = os.getenv("TICKET_FINE_CAR", "Rp20.000,-")
TICKET_FINE_MOTOR = os.getenv("TICKET_FINE_MOTOR", "Rp10.000,-")

# ---------------- MQTT ----------------
MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_KEEPALIVE = int(os.getenv("MQTT_KEEPALIVE", "60"))
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "gate-in-bbb-01")

MQTT_GENERAL_TOPIC = os.getenv(
    "MQTT_GENERAL_TOPIC", "parking/gate-in/general"
)
MQTT_MEMBER_REQUEST_TOPIC = os.getenv(
    "MQTT_MEMBER_REQUEST_TOPIC", "parking/gate-in/member/request"
)
MQTT_MEMBER_RESPONSE_TOPIC = os.getenv(
    "MQTT_MEMBER_RESPONSE_TOPIC", "parking/gate-in/member/response"
)
MQTT_HELP_TOPIC = os.getenv(
    "MQTT_HELP_TOPIC", "parking/gate-in/help"
)

MEMBER_VERIFY_TIMEOUT = float(os.getenv("MEMBER_VERIFY_TIMEOUT", "10"))
EVENT_DEBOUNCE = float(os.getenv("EVENT_DEBOUNCE", "0.5"))

# ---------------- SESSION ----------------
SESSION_SEQUENCE_FILE = os.getenv(
    "SESSION_SEQUENCE_FILE", "./session_sequence.txt"
)
SESSION_SEQUENCE_DIGITS = 4

# How long a vehicle can remain in the "waiting for choice/scan" state.
USER_ACTION_TIMEOUT = float(os.getenv("USER_ACTION_TIMEOUT", "30"))

# ---------------- HELP ----------------
HELP_REASON_BUTTON = os.getenv("HELP_REASON_BUTTON", "tombol bantuan ditekan!")
HELP_REASON_PRINTER = os.getenv("HELP_REASON_PRINTER", "kertas habis, segera isi ulang!")

# ---------------- MESSAGE STORE ----------------
MESSAGE_STORE_DB = os.getenv("MESSAGE_STORE_DB", "./message_store.db")
MESSAGE_RETRY_INTERVAL = float(os.getenv("MESSAGE_RETRY_INTERVAL", "5"))
MESSAGE_MAX_RETRIES = int(os.getenv("MESSAGE_MAX_RETRIES", "100"))
