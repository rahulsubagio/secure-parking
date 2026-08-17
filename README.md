# Parking Gate IN - BBB

Arsitektur:

BBB = Modbus RTU Master
Outseal Mega V3 = Modbus RTU Slave, address 1, 9600 baud.

## Modbus map used by this application

Pymodbus memakai alamat zero-based.

| Outseal | Modbus table | pymodbus address | Fungsi |
|---|---|---:|---|
| R.1 | Coil | 0 | Barrier OPEN command/status |
| R.2 | Coil | 1 | Barrier CLOSE command/status |
| B.1 | Coil | 128 | State proses |
| B.21 | Coil | 148 | BBB_OPEN_REQUEST |
| B.37 | Coil | 164 | Mirror B.1 pada Rung 36 |
| S.1 | Discrete Input | 0 | tombol hijau |
| S.2 | Discrete Input | 1 | tombol kuning |
| S.3 | Discrete Input | 2 | tombol merah |
| S.4 | Discrete Input | 3 | VLD dispenser |
| S.5 | Discrete Input | 4 | VLD barrier |
| S.6 | Discrete Input | 5 | photocell |

## Alur

### Pengunjung umum

S4 -> BBB membuat sesi -> S1/S2 -> publish MQTT -> print tiket -> write B21 -> PLC membuka R1.

Payload:
{
  "sessionNumber": "YYYYMMDDNNNN",
  "vehicleType": "car" atau "motorcycle",
  "entryTime": "yyyy-MM-ddTHH:mm:ss.SSS"
}

### Member

S4 -> scan QR -> MQTT member request -> server mengembalikan success -> jika true write B21.

Payload request:
{
  "requestId": "...",
  "sessionNumber": "YYYYMMDDNNNN",
  "memberId": "...",
  "entryTime": "yyyy-MM-ddTHH:mm:ss.SSS"
}

Response minimal:
{
  "requestId": "...",
  "success": true
}

Jika false/timeout, B21 tidak ditulis.

### Bantuan

S3 event -> MQTT:
{
  "time": "yyyy-MM-ddTHH:mm:ss.SSS"
}

## Instalasi

1. Buat virtual environment:
   python3 -m venv venv
   source venv/bin/activate

2. Install:
   pip install -r requirements.txt

3. Copy konfigurasi:
   cp .env.example .env

4. Sesuaikan:
   PLC_PORT
   SCANNER_DEVICE
   MQTT_HOST
   MQTT topics
   PRINTER settings

5. Letakkan logo Rumah Sakit sebagai:
   ./logo.png

6. Jalankan:
   python3 main.py

## Catatan hardware

Untuk mode production, Outseal SW2 harus di posisi 485 agar RS485 slave aktif. Saat SW2 di posisi 485, USB Outseal tidak aktif untuk programming/online.

BBB menggunakan USB-RS485 ke pin A-S/B-S Outseal.

## Urutan commissioning

1. Test PLC tanpa BBB.
2. Pastikan S6 clear/blocked memberikan status yang benar di Online Monitor.
3. Test baca S1-S6 dari BBB.
4. Test write B21 dengan kendaraan tidak berada di area berbahaya.
5. Pastikan B21 memicu B3/B4/R1.
6. Test S5 -> timer 3 detik.
7. Test S6 blocked -> R2 tidak aktif.
8. Test S6 clear -> R2 aktif.
9. Baru aktifkan MQTT.
10. Baru aktifkan printer dan scanner.


## MQTT configuration update

The BBB connects to the MQTT broker using:

- Host: `secure-parking.seigan.id`
- Port: `1883`
- Username: configured in `.env`
- Protocol: MQTT over TCP on port 1883 (no `https://` prefix)

### Member verification correlation

The BBB no longer generates a UUID/request ID.

The `sessionNumber` is the only transaction/request identifier:

```json
{
  "sessionNumber": "202608170001",
  "memberId": "NIP123456",
  "entryTime": "2026-08-17T10:55:01.123"
}
```

The BBB waits for a response on `MQTT_MEMBER_RESPONSE_TOPIC` and matches it by
`sessionNumber`.

Example response:

```json
{
  "sessionNumber": "202608170001",
  "success": true
}
```

For compatibility, the response handler can also use a returned `requestId`
as a fallback correlation field, but BBB itself never generates or publishes
a separate `requestId`.


## Help request / printer fault

The same MQTT help topic is used for manual assistance and printer problems.

Manual button payload:
```json
{
  "time": "2026-08-17T11:55:00.123",
  "reason": "tombol bantuan ditekan!"
}
```

Printer error / paper-out payload:
```json
{
  "time": "2026-08-17T11:56:00.456",
  "reason": "kertas habis, segera isi ulang!"
}
```

The BBB reports a printer problem if the ESC/POS status query reports paper-end
or if the ticket-printing operation raises an exception. Some printer firmware
does not expose paper status over USB; in that case an actual printer/USB error
is still reported, but a silent physical paper-out cannot be guaranteed to be
detected by software.

## Member payload

The member request uses `memberCode` (not `memberId`):
```json
{
  "sessionNumber": "202608170001",
  "memberCode": "NIP123456",
  "entryTime": "2026-08-17T11:57:00.123"
}
```
