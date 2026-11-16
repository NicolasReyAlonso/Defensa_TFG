#!/usr/bin/env python3
"""Mando de Wii como puntero de presentaciones.

    python3 presenter.py --pair         # solo macOS: una vez por mando (botón SYNC)
    python3 presenter.py --calibrate    # una vez por sala/posición
    python3 presenter.py                # presentar (Home para salir)

Linux: solo biblioteca estándar. Habla con el mando por sockets L2CAP de
Bluetooth e inyecta ratón/teclado por /dev/uinput (o pynput en X11).
macOS: lee el mando como dispositivo HID (hidapi) e inyecta eventos con
Quartz (pyobjc); el emparejamiento replica WiimotePair, de Dolphin.
No instala nada ni deja nada en marcha al salir.
"""
import argparse
import collections
import datetime
import fcntl
import json
import math
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import time

# ───────────────────────── Configuración editable ─────────────────────────

# Botones del mando (máscaras sobre los dos bytes de botones del protocolo).
LEFT, RIGHT, DOWN, UP, PLUS = 0x0100, 0x0200, 0x0400, 0x0800, 0x1000
TWO, ONE, B, A, MINUS, HOME = 0x0001, 0x0002, 0x0004, 0x0008, 0x0010, 0x0080

# Mapeo botón -> acción. Acciones posibles:
#   ("key", nombre)     pulsación de tecla; nombres en KEY_CODES
#   ("hold", nombre)    tecla mantenida mientras dure la pulsación
#   ("click", "left")   botón de ratón mantenido mientras dure la pulsación
#   ("quit",)           salir limpiamente
#   None                sin asignar
BUTTON_MAP = {
    RIGHT: ("key", "right"),    # siguiente diapositiva
    LEFT:  ("key", "left"),     # anterior
    A:     ("click", "left"),
    HOME:  ("quit",),
    B:     ("hold", "l"),       # puntero láser (lo dibuja la presentación con la tecla L)
    UP: None, DOWN: None, PLUS: None, MINUS: None, ONE: None, TWO: None,
}

CONFIG = {
    "smoothing": "ema",   # "ema" (exponencial) o "mean" (media móvil)
    "alpha": 0.25,        # ema: 0..1, más bajo = más suave y más retardo
    "window": 8,          # mean: nº de muestras
    "lost_frames": 30,    # frames (~100 Hz) sin ver la barra antes de darla por perdida
    "ir_sensitivity": 3,  # 1..5, como en el menú de la Wii
    "udp_port": 9999,
}

HERE = os.path.dirname(os.path.abspath(__file__))
CALIBRATION_FILE = os.path.join(HERE, "calibration.json")
CORNERS = ["superior izquierda", "superior derecha",
           "inferior derecha", "inferior izquierda"]

# nombre -> código de tecla de Linux (input-event-codes.h)
KEY_CODES = {"left": 105, "right": 106, "up": 103, "down": 108, "pageup": 104,
             "pagedown": 109, "esc": 1, "enter": 28, "space": 57, "f5": 63,
             "b": 48, "l": 38, "home": 102, "end": 107}
BTN_CODES = {"left": 0x110, "right": 0x111}
# Los mismos nombres -> código de tecla virtual de macOS (HIToolbox/Events.h)
MAC_KEY_CODES = {"left": 123, "right": 124, "up": 126, "down": 125, "pageup": 116,
                 "pagedown": 121, "esc": 53, "enter": 36, "space": 49, "f5": 96,
                 "b": 11, "l": 37, "home": 115, "end": 119}

MAC_DEPS = ("Faltan las dependencias de macOS. Desde la carpeta del script:\n"
            "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt\n"
            "y lánzalo con .venv/bin/python presenter.py")

# ───────────────────────────── Mando de Wii ───────────────────────────────

IR_W, IR_H = 1024, 768
# Bloques de sensibilidad de la cámara IR (niveles 1-5 de la Wii).
IR_SENS = {
    1: (bytes.fromhex("0200007101006400fe"), bytes.fromhex("fd05")),
    2: (bytes.fromhex("0200007101009600b4"), bytes.fromhex("b304")),
    3: (bytes.fromhex("020000710100aa0064"), bytes.fromhex("6303")),
    4: (bytes.fromhex("020000710100c80036"), bytes.fromhex("3503")),
    5: (bytes.fromhex("070000710100720020"), bytes.fromhex("1f03")),
}


def find_wiimote(scan_time):
    """Busca un mando en modo visible (1+2) usando bluetoothctl. Devuelve la MAC."""
    print(f"Buscando mando {scan_time}s... pulsa 1+2 en el mando (LEDs parpadeando)")
    subprocess.run(["bluetoothctl", "--timeout", str(scan_time), "scan", "on"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out = subprocess.run(["bluetoothctl", "devices"], capture_output=True,
                         text=True).stdout
    for line in out.splitlines():
        m = re.match(r"Device ([0-9A-F:]{17}) (.*)", line)
        if m and "Nintendo RVL" in m.group(2):
            print(f"Encontrado {m.group(2)} [{m.group(1)}]")
            return m.group(1)
    return None


class Wiimote:
    """Protocolo del mando, común a los dos transportes (L2CAP en Linux, HID en macOS).

    Los informes de entrada se entregan siempre con la cabecera Bluetooth 0xA1
    delante, que es lo que espera parse_report.
    """

    def __init__(self):
        self.rumble = 0
        self._leds = 0

    def _tx(self, report):
        """Envía id de informe + datos, sin cabecera Bluetooth."""
        raise NotImplementedError

    def _rx(self):
        """Devuelve un informe con cabecera 0xA1, o None si no llegó nada a tiempo."""
        raise NotImplementedError

    def _close_transport(self):
        raise NotImplementedError

    def send(self, report, *payload):
        p = bytearray(payload)
        p[0] |= self.rumble          # el bit 0 de todo informe de salida es la vibración
        self._tx(bytes([report]) + p)

    def leds(self, n):
        """Enciende los n primeros LEDs (0-4)."""
        self._leds = ((1 << n) - 1) << 4
        self.send(0x11, self._leds)

    def buzz(self, seconds=0.12):
        self.rumble = 1
        self.send(0x11, self._leds)
        time.sleep(seconds)
        self.rumble = 0
        self.send(0x11, self._leds)

    def _write_reg(self, addr, data):
        self.send(0x16, 0x04, addr >> 16 & 0xFF, addr >> 8 & 0xFF, addr & 0xFF,
                  len(data), *data.ljust(16, b"\0"))
        time.sleep(0.05)

    def start(self, sensitivity):
        """Activa la cámara IR y el informe continuo 0x33 (botones+acel+IR extendido)."""
        block1, block2 = IR_SENS[sensitivity]
        self.send(0x13, 0x04); time.sleep(0.05)
        self.send(0x1A, 0x04); time.sleep(0.05)
        self._write_reg(0xB00030, b"\x08")
        self._write_reg(0xB00000, block1)
        self._write_reg(0xB0001A, block2)
        self._write_reg(0xB00033, b"\x03")      # modo extendido: 3 bytes por punto
        self._write_reg(0xB00030, b"\x08")
        self.set_report_mode()
        self.send(0x15, 0)                      # pide estado (batería)

    def set_report_mode(self):
        self.send(0x12, 0x04, 0x33)             # 0x04 = continuo

    def read(self):
        """Devuelve (botones, puntos_ir | None, bateria | None), o None si no llegó nada."""
        d = self._rx()
        return None if d is None else parse_report(d, self)

    def close(self):
        try:
            self.rumble = 0
            self.send(0x13, 0x00)
            self.send(0x1A, 0x00)
            self.leds(0)
        except OSError:
            pass
        self._close_transport()


class L2capWiimote(Wiimote):
    """Linux: HID sobre L2CAP (PSM 0x11 control, 0x13 datos), como hace libcwiid."""

    def __init__(self, mac):
        super().__init__()
        self.ctl = self._connect(mac, 0x11)
        self.data = self._connect(mac, 0x13)
        self.data.settimeout(0.5)

    @staticmethod
    def _connect(mac, psm):
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                          socket.BTPROTO_L2CAP)
        s.settimeout(10)
        s.connect((mac, psm))
        return s

    def _tx(self, report):
        # 0xA2 por el canal de datos funciona también en los RVL-CNT-01-TR.
        self.data.send(b"\xA2" + report)

    def _rx(self):
        try:
            d = self.data.recv(32)
        except socket.timeout:
            return None
        if not d:
            raise ConnectionError("el mando cerró la conexión")
        return d

    def _close_transport(self):
        for s in (self.data, self.ctl):
            try:
                s.close()
            except OSError:
                pass


class HidWiimote(Wiimote):
    """macOS: el sistema gestiona los canales Bluetooth y expone el mando como HID.

    Abrir L2CAP directamente (lo que hace Linux) dejó de funcionar en macOS 12;
    Dolphin también usa hidapi en Mac. Sin la cabecera 0xA1/0xA2: el primer
    byte es el id de informe, como espera hidapi.
    """

    def __init__(self, path):
        super().__init__()
        import hid
        self.dev = hid.device()
        self.dev.open_path(path)

    def _tx(self, report):
        try:
            ok = self.dev.write(report) >= 0
        except (OSError, ValueError):
            ok = False
        if not ok:
            raise ConnectionError(f"no se pudo escribir en el mando ({self.dev.error()})")

    def _rx(self):
        try:
            d = self.dev.read(32, 500)
        except (OSError, ValueError) as e:
            raise ConnectionError(f"error leyendo del mando ({e})") from e
        return b"\xA1" + bytes(d) if d else None

    def _close_transport(self):
        self.dev.close()


WIIMOTE_HID_IDS = {(0x057E, 0x0306), (0x057E, 0x0330)}   # RVL-CNT-01 y -TR


def find_hid_wiimote(wait):
    """macOS: espera a que un mando ya emparejado aparezca como HID. Devuelve su ruta."""
    try:
        import hid
    except ImportError:
        sys.exit(MAC_DEPS)
    deadline, told = time.monotonic() + wait, False
    while time.monotonic() < deadline:
        # macOS a veces publica los HID Bluetooth con VID/PID 0: también por nombre.
        for d in hid.enumerate():
            if ((d["vendor_id"], d["product_id"]) in WIIMOTE_HID_IDS
                    or (d["product_string"] or "").startswith("Nintendo RVL-CNT")):
                print(f"Encontrado {d['product_string'] or 'mando de Wii'}")
                return d["path"]
        if not told:
            print(f"Esperando al mando {wait}s: pulsa cualquier botón para que se conecte.")
            told = True
        time.sleep(0.5)
    return None


def mac_pair(timeout=90):
    """macOS: empareja el mando con el PIN del botón SYNC, como WiimotePair (Dolphin).

    El PIN de SYNC es la dirección Bluetooth del Mac al revés y en binario. La
    API pública solo acepta PIN de texto, así que se entrega con las mismas dos
    llamadas privadas de IOBluetooth que usa WiimotePair.
    """
    try:
        import objc
        from CoreBluetooth import CBCentralManager
        from Foundation import NSDate, NSNumber, NSObject, NSRunLoop
        from IOBluetooth import (IOBluetoothDeviceInquiry, IOBluetoothDevicePair,
                                 IOBluetoothHostController)
    except ImportError:
        sys.exit(MAC_DEPS)

    st = {"bt": None, "pair": None, "done": False, "ok": False, "error": None}

    def run(until, seconds):
        end = time.monotonic() + seconds
        while not until() and time.monotonic() < end:
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))

    def ioreturn(err):
        return f"IOReturn 0x{err & 0xFFFFFFFF:08X}"

    def found(inquiry, dev):
        name = dev.name() or ""
        if st["pair"] or st["done"] or "Nintendo RVL-CNT-01" not in name:
            return
        if dev.isPaired():
            print(f"{name} [{dev.addressString()}] ya está emparejado con este Mac.\n"
                  "Pulsa un botón del mando para conectarlo. Si no conecta, olvídalo en\n"
                  "Ajustes > Bluetooth y repite --pair.")
            st["done"] = st["ok"] = True
            return
        print(f"Encontrado {name} [{dev.addressString()}]. Emparejando...")
        inquiry.stop()
        pair = IOBluetoothDevicePair.pairWithDevice_(dev)
        pair.setDelegate_(delegate)
        pair.setUserDefinedPincode_(True)       # privada: que nos pida siempre el PIN
        st["pair"] = pair
        err = pair.start()
        if err:
            st["error"], st["done"] = f"no se pudo iniciar ({ioreturn(err)})", True

    class WiiPairDelegate(NSObject):
        @objc.typedSelector(b"v@:@")
        def centralManagerDidUpdateState_(self, manager):
            st["bt"] = manager.state()

        @objc.typedSelector(b"v@:@@")
        def deviceInquiryDeviceFound_device_(self, inquiry, dev):
            found(inquiry, dev)

        @objc.typedSelector(b"v@:@@I")
        def deviceInquiryDeviceNameUpdated_device_devicesRemaining_(self, inquiry, dev, n):
            found(inquiry, dev)

        @objc.typedSelector(b"v@:@iZ")
        def deviceInquiryComplete_error_aborted_(self, inquiry, error, aborted):
            if not aborted and not st["pair"]:  # la búsqueda caduca sola: otra vuelta
                inquiry.clearFoundDevices()
                inquiry.start()

        @objc.typedSelector(b"v@:@")
        def devicePairingPINCodeRequest_(self, pair):
            host = IOBluetoothHostController.defaultController().addressAsString()
            pin = bytes.fromhex(re.sub("[^0-9A-Fa-f]", "", host))[::-1]
            try:
                coordinator = objc.lookUpClass("IOBluetoothCoreBluetoothCoordinator")
                coordinator.sharedInstance().pairPeer_forType_withKey_(
                    pair.device().classicPeer(), pair.currentPairingType(),
                    NSNumber.numberWithUnsignedLongLong_(int.from_bytes(pin, "little")))
            except Exception as e:      # ninguna excepción debe escapar hacia Objective-C
                st["error"], st["done"] = f"macOS rechazó el PIN ({e})", True

        @objc.typedSelector(b"v@:@i")
        def devicePairingFinished_error_(self, pair, error):
            if error:
                st["error"] = ioreturn(error)
            st["ok"], st["done"] = not error, True

    delegate = WiiPairDelegate.alloc().init()
    # CoreBluetooth solo se usa para saber si hay Bluetooth y permiso (y para que
    # macOS lo pida la primera vez); el emparejamiento es Bluetooth clásico.
    manager = CBCentralManager.alloc().initWithDelegate_queue_(delegate, None)
    run(lambda: st["bt"] not in (None, 0, 1), 60)          # 0 desconocido, 1 reiniciando
    if st["bt"] == 3:
        sys.exit("Sin permiso de Bluetooth. Concédelo a la app desde la que lanzas el script\n"
                 "(Terminal, VS Code...) en Ajustes > Privacidad y seguridad > Bluetooth.")
    if st["bt"] != 5:
        sys.exit("El Bluetooth está apagado." if st["bt"] == 4
                 else f"Bluetooth no disponible (estado {st['bt']}).")

    inquiry = IOBluetoothDeviceInquiry.inquiryWithDelegate_(delegate)
    inquiry.setSearchType_(1)                               # kIOBluetoothDeviceSearchClassic
    print("Abre la tapa de las pilas y pulsa una vez el botón rojo SYNC (no 1+2).\n"
          f"Buscando el mando durante {timeout}s... Ctrl+C cancela.")
    inquiry.start()
    try:
        run(lambda: st["done"], timeout)
    finally:
        inquiry.stop()
        if st["pair"]:
            st["pair"].stop()
        del manager
    if not st["done"]:
        sys.exit("No apareció ningún mando. Pulsa SYNC justo antes de lanzar --pair.")
    if not st["ok"]:
        sys.exit(f"Emparejamiento fallido: {st['error']}.\nVuelve a intentarlo; si se repite, "
                 "reinicia el Bluetooth (sudo pkill bluetoothd) o el Mac.")
    if st["pair"]:
        print("Emparejado. Pulsa cualquier botón del mando para conectarlo (o 'Conectar'\n"
              "en Ajustes > Bluetooth) y lanza: python3 presenter.py --calibrate")


def parse_report(d, wm=None):
    if len(d) < 4 or d[0] != 0xA1 or d[1] == 0x3D:
        return None
    buttons = ((d[2] & 0x1F) << 8) | (d[3] & 0x9F)
    dots = battery = None
    if d[1] == 0x33 and len(d) >= 19:
        dots = []
        for i in range(7, 19, 3):
            x, y, s = d[i], d[i + 1], d[i + 2]
            if (x, y, s) == (0xFF, 0xFF, 0xFF):
                continue
            x |= (s & 0x30) << 4
            y |= (s & 0xC0) << 2
            # La cámara ve la escena en espejo: se invierte X aquí.
            dots.append((IR_W - 1 - x, y, s & 0x0F))
    elif d[1] == 0x20 and len(d) >= 8:
        battery = d[7]
        if wm:                       # tras un informe de estado hay que re-fijar el modo
            wm.set_report_mode()
    return buttons, dots, battery

# ──────────────────────── Seguimiento y calibración ───────────────────────


class Tracker:
    """Convierte los puntos IR en un punto de apuntado estable en coords. de cámara.

    Usa el punto medio de los dos LEDs de la barra, corregido por el giro de
    muñeca. Si solo se ve uno, extrapola con la última geometría conocida; si
    no se ve ninguno, no devuelve nada (el cursor se queda quieto).
    """

    def __init__(self, lost_frames):
        self.lost_frames = lost_frames
        self.a = self.b = None      # últimos dos puntos, a a la izquierda
        self.missing = 0

    def update(self, dots):
        pts = [(x, y) for x, y, _ in dots]
        if len(pts) >= 2:
            if len(pts) > 2:
                pts = self._pick_pair(dots)
            self.a, self.b = sorted(pts)
        elif len(pts) == 1 and self.a is not None:
            p = pts[0]
            ref = min((self.a, self.b), key=lambda q: math.dist(p, q))
            dx, dy = p[0] - ref[0], p[1] - ref[1]
            self.a = (self.a[0] + dx, self.a[1] + dy)
            self.b = (self.b[0] + dx, self.b[1] + dy)
        else:
            self.missing += 1
            if self.missing > self.lost_frames:
                self.a = self.b = None      # geometría caducada: exigir 2 puntos
            return None
        self.missing = 0
        (ax, ay), (bx, by) = self.a, self.b
        ang = math.atan2(by - ay, bx - ax)
        mx, my = (ax + bx) / 2 - IR_W / 2, (ay + by) / 2 - IR_H / 2
        c, s = math.cos(-ang), math.sin(-ang)
        return (mx * c - my * s + IR_W / 2, mx * s + my * c + IR_H / 2)

    def _pick_pair(self, dots):
        if self.a is not None:      # los más cercanos a los ya seguidos
            pts = [(x, y) for x, y, _ in dots]
            pa = min(pts, key=lambda q: math.dist(q, self.a))
            pts.remove(pa)
            return [pa, min(pts, key=lambda q: math.dist(q, self.b))]
        big = sorted(dots, key=lambda d: -d[2])[:2]
        return [(x, y) for x, y, _ in big]


def homography(src, dst):
    """Matriz 3x3 (h33=1) que lleva los 4 puntos src a dst. Gauss, sin numpy."""
    m = []
    for (x, y), (u, v) in zip(src, dst):
        m.append([x, y, 1, 0, 0, 0, -u * x, -u * y, u])
        m.append([0, 0, 0, x, y, 1, -v * x, -v * y, v])
    n = 8
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            raise ValueError("puntos de calibración degenerados")
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                m[r] = [a - f * b for a, b in zip(m[r], m[col])]
    return [m[i][8] / m[i][i] for i in range(n)] + [1.0]


def apply_h(h, p):
    x, y = p
    w = h[6] * x + h[7] * y + h[8]
    return ((h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w)


def is_convex(q):
    signs = []
    for i in range(4):
        (ax, ay), (bx, by), (cx, cy) = q[i], q[(i + 1) % 4], q[(i + 2) % 4]
        signs.append((bx - ax) * (cy - by) - (by - ay) * (cx - bx) > 0)
    return all(signs) or not any(signs)


def load_calibration():
    try:
        with open(CALIBRATION_FILE) as f:
            corners = json.load(f)["corners"]
    except FileNotFoundError:
        sys.exit("No hay calibración. Ejecuta primero: python3 presenter.py --calibrate")
    return homography(corners, [(0, 0), (1, 0), (1, 1), (0, 1)])


class Smoother:
    def __init__(self, mode, alpha, window):
        self.mode, self.alpha = mode, alpha
        self.buf = collections.deque(maxlen=window)
        self.state = None

    def __call__(self, p):
        # El estado se conserva tras perder la barra: al recuperarla el cursor
        # se desliza hasta la nueva posición en lugar de saltar.
        if self.mode == "mean":
            self.buf.append(p)
            n = len(self.buf)
            return (sum(q[0] for q in self.buf) / n, sum(q[1] for q in self.buf) / n)
        if self.state is None:
            self.state = p
        else:
            a = self.alpha
            self.state = (self.state[0] + a * (p[0] - self.state[0]),
                          self.state[1] + a * (p[1] - self.state[1]))
        return self.state

# ─────────────────────── Salida: ratón y teclado ──────────────────────────
# Todos los backends reciben la posición normalizada (0..1, 0..1).


class UInput:
    """Teclado y puntero absoluto virtuales vía /dev/uinput. Vale en Wayland y X11."""
    EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
    UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT = 0x40045564, 0x40045565, 0x40045567
    UI_DEV_SETUP, UI_ABS_SETUP = 0x405C5503, 0x401C5504
    UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502
    ABS_MAX = 65535

    def __init__(self):
        try:
            self.kbd = self._device("Wii presenter keyboard", KEY_CODES.values(), False)
            self.ptr = self._device("Wii presenter pointer", BTN_CODES.values(), True)
        except PermissionError:
            sys.exit("Sin permiso sobre /dev/uinput. Dalo solo hasta el próximo "
                     "reinicio con:\n  sudo setfacl -m u:$USER:rw /dev/uinput")
        self.held = set()
        time.sleep(0.6)             # el compositor tarda en adoptar los dispositivos

    def _device(self, name, keys, absolute):
        fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(fd, self.UI_SET_EVBIT, self.EV_KEY)
        for k in keys:
            fcntl.ioctl(fd, self.UI_SET_KEYBIT, k)
        if absolute:
            fcntl.ioctl(fd, self.UI_SET_EVBIT, self.EV_ABS)
            for axis in (0, 1):     # ABS_X, ABS_Y
                fcntl.ioctl(fd, self.UI_SET_ABSBIT, axis)
                fcntl.ioctl(fd, self.UI_ABS_SETUP,
                            struct.pack("H2x6i", axis, 0, 0, self.ABS_MAX, 0, 0, 0))
        fcntl.ioctl(fd, self.UI_DEV_SETUP,
                    struct.pack("4H80sI", 0x06, 0x1234, 0x5678, 1, name.encode(), 0))
        fcntl.ioctl(fd, self.UI_DEV_CREATE)
        return fd

    @staticmethod
    def _emit(fd, *events):
        buf = b"".join(struct.pack("llHHi", 0, 0, t, c, v) for t, c, v in events)
        os.write(fd, buf + struct.pack("llHHi", 0, 0, 0, 0, 0))    # SYN_REPORT

    def move(self, u, v):
        self._emit(self.ptr, (self.EV_ABS, 0, round(u * self.ABS_MAX)),
                   (self.EV_ABS, 1, round(v * self.ABS_MAX)))

    def key(self, name):
        self._emit(self.kbd, (self.EV_KEY, KEY_CODES[name], 1))
        self._emit(self.kbd, (self.EV_KEY, KEY_CODES[name], 0))

    def hold(self, name, down):
        self._set(self.kbd, KEY_CODES[name], down)

    def button(self, name, down):
        self._set(self.ptr, BTN_CODES[name], down)

    def _set(self, fd, code, down):
        (self.held.add if down else self.held.discard)((fd, code))
        self._emit(fd, (self.EV_KEY, code, int(down)))

    def close(self):
        for fd, code in list(self.held):
            self._set(fd, code, False)
        for fd in (self.kbd, self.ptr):
            fcntl.ioctl(fd, self.UI_DEV_DESTROY)
            os.close(fd)


def pick_display(Q, number):
    """macOS: (x, y, ancho, alto) en puntos de la pantalla `number` (1..n).

    Por defecto, la principal (la de la barra de menús). Con varias pantallas
    las lista, para poder elegir la del proyector con --display.
    """
    _, ids, _ = Q.CGGetActiveDisplayList(16, None, None)
    main = Q.CGMainDisplayID()
    if number is None:
        number = ids.index(main) + 1 if main in ids else 1
    if not 1 <= number <= len(ids):
        sys.exit(f"--display {number}: solo hay {len(ids)} pantalla(s).")
    if len(ids) > 1:
        for i, d in enumerate(ids, 1):
            b = Q.CGDisplayBounds(d)
            tags = [t for t, on in (("integrada", Q.CGDisplayIsBuiltin(d)),
                                    ("principal", d == main),
                                    ("en espejo", Q.CGDisplayMirrorsDisplay(d))) if on]
            print(f" {'>' if i == number else ' '} pantalla {i}: "
                  f"{b.size.width:.0f}x{b.size.height:.0f} en ({b.origin.x:.0f}, "
                  f"{b.origin.y:.0f}) {', '.join(tags)}")
        print("   (elige otra con --display N)")
    b = Q.CGDisplayBounds(ids[number - 1])
    return b.origin.x, b.origin.y, b.size.width, b.size.height


class Quartz:
    """macOS: ratón y teclado con CGEvent (pyobjc). Requiere permiso de Accesibilidad."""

    def __init__(self, display):
        try:
            import Quartz as Q
        except ImportError:
            sys.exit(MAC_DEPS)
        self.Q = Q
        if not Q.CGPreflightPostEventAccess():
            Q.CGRequestPostEventAccess()        # muestra el aviso del sistema
            sys.exit("Sin permiso para controlar ratón y teclado: macOS descartaría los "
                     "eventos.\nActiva la app desde la que lanzas el script (Terminal, "
                     "VS Code...) en\nAjustes > Privacidad y seguridad > Accesibilidad "
                     "y vuelve a lanzarlo.")
        self.x0, self.y0, self.w, self.h = pick_display(Q, display)
        self.mouse = {   # botón -> (pulsar, soltar, arrastrar, número)
            "left": (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp,
                     Q.kCGEventLeftMouseDragged, Q.kCGMouseButtonLeft),
            "right": (Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp,
                      Q.kCGEventRightMouseDragged, Q.kCGMouseButtonRight)}
        self.dragging = None
        self.held = set()

    def _post(self, event):
        self.Q.CGEventPost(self.Q.kCGHIDEventTap, event)

    def move(self, u, v):
        Q = self.Q
        p = (self.x0 + u * (self.w - 1), self.y0 + v * (self.h - 1))
        if self.dragging:       # con un botón pulsado macOS espera "dragged", no "moved"
            kind, btn = self.mouse[self.dragging][2:]
        else:
            kind, btn = Q.kCGEventMouseMoved, Q.kCGMouseButtonLeft
        self._post(Q.CGEventCreateMouseEvent(None, kind, p, btn))

    def _key(self, name, down):
        self._post(self.Q.CGEventCreateKeyboardEvent(None, MAC_KEY_CODES[name], down))

    def key(self, name):
        self._key(name, True)
        self._key(name, False)

    def hold(self, name, down):
        (self.held.add if down else self.held.discard)(("hold", name))
        self._key(name, down)

    def button(self, name, down):
        Q = self.Q
        press, release, _, btn = self.mouse[name]
        (self.held.add if down else self.held.discard)(("button", name))
        self.dragging = name if down else None
        here = Q.CGEventGetLocation(Q.CGEventCreate(None))
        event = Q.CGEventCreateMouseEvent(None, press if down else release, here, btn)
        Q.CGEventSetIntegerValueField(event, Q.kCGMouseEventClickState, 1)
        self._post(event)

    def close(self):
        for kind, name in list(self.held):
            getattr(self, kind)(name, False)


class Pynput:
    """Para X11 (requiere `pip install pynput`). En macOS es mejor el backend quartz."""

    def __init__(self, screen):
        from pynput import keyboard, mouse
        self.kb, self.mouse = keyboard.Controller(), mouse.Controller()
        self.Key, self.Button = keyboard.Key, mouse.Button
        self.w, self.h = screen or self._screen_size()
        self.held = set()

    @staticmethod
    def _screen_size():
        if sys.platform == "darwin":
            import Quartz
            b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
            return int(b.size.width), int(b.size.height)
        from Xlib import display
        s = display.Display().screen()
        return s.width_in_pixels, s.height_in_pixels

    def move(self, u, v):
        self.mouse.position = (u * (self.w - 1), v * (self.h - 1))

    def _key(self, name):
        k = {"pageup": "page_up", "pagedown": "page_down"}.get(name, name)
        return getattr(self.Key, k, k)

    def key(self, name):
        self.kb.tap(self._key(name))

    def hold(self, name, down):
        (self.held.add if down else self.held.discard)(("hold", name))
        self.kb.press(self._key(name)) if down else self.kb.release(self._key(name))

    def button(self, name, down):
        (self.held.add if down else self.held.discard)(("button", name))
        btn = getattr(self.Button, name)
        self.mouse.press(btn) if down else self.mouse.release(btn)

    def close(self):
        for kind, name in list(self.held):
            getattr(self, kind)(name, False)


class Udp:
    """Envía los eventos a otro equipo que ejecute `presenter.py --listen`."""

    def __init__(self, target):
        host, _, port = target.partition(":")
        self.addr = (host, int(port or CONFIG["udp_port"]))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.held = set()

    def _tx(self, *msg):
        self.sock.sendto(json.dumps(msg).encode(), self.addr)

    def move(self, u, v):
        self._tx("move", round(u, 5), round(v, 5))

    def key(self, name):
        self._tx("key", name)

    def hold(self, name, down):
        (self.held.add if down else self.held.discard)(("hold", name))
        self._tx("hold", name, down)

    def button(self, name, down):
        (self.held.add if down else self.held.discard)(("button", name))
        self._tx("button", name, down)

    def close(self):
        for kind, name in list(self.held):
            getattr(self, kind)(name, False)
        self.sock.close()


class Dry:
    """No toca el sistema: imprime lo que haría."""

    def move(self, u, v):
        print(f"\rcursor {u:6.3f} {v:6.3f}", end="", flush=True)

    def key(self, name):
        print(f"\ntecla {name}")

    def hold(self, name, down):
        print(f"\ntecla {name} {'abajo' if down else 'arriba'}")

    def button(self, name, down):
        print(f"\nratón {name} {'abajo' if down else 'arriba'}")

    def close(self):
        print()


def make_backend(args):
    kind = args.backend
    if args.send:
        return Udp(args.send)
    if kind == "auto":
        x11 = os.environ.get("XDG_SESSION_TYPE") == "x11"
        kind = "quartz" if sys.platform == "darwin" else "pynput" if x11 else "uinput"
    if kind == "quartz":
        return Quartz(args.display)
    if kind == "pynput":
        return Pynput(args.screen)
    return Dry() if kind == "dry" else UInput()

# ───────────────────────────── Modos de uso ───────────────────────────────


def connect(args):
    if sys.platform == "darwin":
        path = find_hid_wiimote(args.scan_time or 20)
        if not path:
            sys.exit("No hay ningún mando conectado al Mac.\n"
                     "  - Si no lo has emparejado nunca: python3 presenter.py --pair\n"
                     "  - Si ya lo está: pulsa un botón del mando o 'Conectar' en "
                     "Ajustes > Bluetooth.")
        try:
            wm = HidWiimote(path)
        except OSError as e:
            sys.exit(f"No se pudo abrir el mando: {e}")
    else:
        mac = args.mac or find_wiimote(args.scan_time or 6)
        if not mac:
            sys.exit("No se encontró ningún mando. Pulsa 1+2 y reintenta, o usa --mac.")
        try:
            wm = L2capWiimote(mac)
        except OSError as e:
            sys.exit(f"No se pudo conectar con {mac}: {e}\n"
                     "Vuelve a pulsar 1+2 justo antes de lanzar el programa.")
    try:
        wm.start(CONFIG["ir_sensitivity"])
    except OSError:
        wm.close()
        raise
    return wm


def reports(wm):
    """Itera informes del mando; termina si deja de responder."""
    silent = 0
    while True:
        r = wm.read()
        if r is None:
            silent += 1
            if silent > 10:
                raise ConnectionError("el mando no responde (¿apagado o sin pilas?)")
            continue
        silent = 0
        buttons, dots, battery = r
        if battery is not None:
            print(f"Batería: {battery * 100 // 200}%")
        yield buttons, dots


def calibrate(wm):
    tracker = Tracker(CONFIG["lost_frames"])
    recent = collections.deque(maxlen=10)
    corners, prev = [], 0
    print("Calibración: colócate donde vayas a presentar. Home cancela.")

    def prompt():
        wm.leds(len(corners) + 1)
        print(f"\n[{len(corners) + 1}/4] Apunta a la esquina {CORNERS[len(corners)]} y pulsa A")

    prompt()
    for buttons, dots in reports(wm):
        if dots is not None:
            p = tracker.update(dots)
            recent.append(p) if p else recent.clear()
            print(f"\r  puntos IR visibles: {len(dots)} ", end="", flush=True)
        pressed, prev = buttons & ~prev, buttons
        if pressed & HOME:
            print("\nCalibración cancelada.")
            return
        if not pressed & A:
            continue
        if len(recent) < 5:
            print("\n  No veo la barra de sensores desde ahí; acércate al centro o aléjate.")
            continue
        corners.append([sum(q[0] for q in recent) / len(recent),
                        sum(q[1] for q in recent) / len(recent)])
        wm.buzz()
        if len(corners) < 4:
            prompt()
            continue
        if not is_convex(corners):
            print("\n  Las esquinas no forman un cuadrilátero válido. Empezamos de nuevo.")
            corners.clear()
            prompt()
            continue
        with open(CALIBRATION_FILE, "w") as f:
            json.dump({"corners": corners, "order": "tl,tr,br,bl",
                       "created": datetime.datetime.now().isoformat(timespec="seconds")},
                      f, indent=2)
        print(f"\nCalibración guardada en {CALIBRATION_FILE}")
        return


def present(wm, out):
    h = load_calibration()
    tracker = Tracker(CONFIG["lost_frames"])
    smooth = Smoother(CONFIG["smoothing"], CONFIG["alpha"], CONFIG["window"])
    prev = 0
    wm.leds(1)
    print("Listo. Home para salir.")
    for buttons, dots in reports(wm):
        if dots is not None:
            p = tracker.update(dots)
            if p:
                u, v = apply_h(h, p)
                out.move(*smooth((min(max(u, 0.0), 1.0), min(max(v, 0.0), 1.0))))
        changed, prev = buttons ^ prev, buttons
        for mask, action in BUTTON_MAP.items():
            if not action or not changed & mask:
                continue
            down = bool(buttons & mask)
            if action[0] == "quit" and down:
                return
            if action[0] == "key" and down:
                out.key(action[1])
            elif action[0] == "hold":
                out.hold(action[1], down)
            elif action[0] == "click":
                out.button(action[1], down)


def listen(args):
    """Receptor UDP: aplica en este equipo los eventos de otro `--send`."""
    out = make_backend(args)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.listen))
    print(f"Escuchando en UDP {args.listen}. Ctrl+C para salir.")
    try:
        while True:
            kind, *rest = json.loads(sock.recv(256))
            if kind == "move":
                out.move(float(rest[0]), float(rest[1]))
            elif kind == "key" and rest[0] in KEY_CODES:
                out.key(rest[0])
            elif kind == "hold" and rest[0] in KEY_CODES:
                out.hold(rest[0], bool(rest[1]))
            elif kind == "button" and rest[0] in BTN_CODES:
                out.button(rest[0], bool(rest[1]))
    finally:
        out.close()
        sock.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pair", action="store_true",
                    help="macOS: emparejar el mando con el botón SYNC (una vez por mando)")
    ap.add_argument("--calibrate", action="store_true", help="calibrar las 4 esquinas")
    ap.add_argument("--mac", help="Linux: MAC del mando (omite la búsqueda)")
    ap.add_argument("--scan-time", type=int,
                    help="segundos buscando el mando (Linux: 6) o esperando a que "
                         "se conecte (macOS: 20)")
    ap.add_argument("--backend", choices=["auto", "uinput", "quartz", "pynput", "dry"],
                    default="auto")
    ap.add_argument("--display", type=int, metavar="N",
                    help="macOS: pantalla donde mover el cursor (se listan al arrancar)")
    ap.add_argument("--screen", type=lambda s: tuple(map(int, s.lower().split("x"))),
                    help="resolución ANCHOxALTO (solo pynput, si no se detecta)")
    ap.add_argument("--smoothing", choices=["ema", "mean"])
    ap.add_argument("--alpha", type=float)
    ap.add_argument("--window", type=int)
    ap.add_argument("--lost-frames", type=int, dest="lost_frames")
    ap.add_argument("--send", metavar="HOST[:PUERTO]", help="enviar eventos por UDP")
    ap.add_argument("--listen", metavar="PUERTO", type=int, nargs="?",
                    const=CONFIG["udp_port"], help="recibir eventos por UDP")
    args = ap.parse_args()
    for k in ("smoothing", "alpha", "window", "lost_frames"):
        if getattr(args, k) is not None:
            CONFIG[k] = getattr(args, k)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        if args.pair:
            if sys.platform != "darwin":
                sys.exit("--pair solo hace falta en macOS; en Linux basta con pulsar 1+2 "
                         "al lanzar el script.")
            return mac_pair()
        if args.listen:
            return listen(args)
        if not args.calibrate:
            load_calibration()              # fallar pronto, antes de emparejar
        out = None if args.calibrate else make_backend(args)
        wm = None
        try:
            wm = connect(args)
            calibrate(wm) if args.calibrate else present(wm, out)
        except ConnectionError as e:
            print(f"\nConexión perdida: {e}")
        finally:
            if out:
                out.close()                 # suelta teclas/botones pulsados
            if wm:
                wm.close()                  # apaga IR y LEDs y libera Bluetooth
    except KeyboardInterrupt:
        pass
    print("Fin.")


if __name__ == "__main__":
    main()
