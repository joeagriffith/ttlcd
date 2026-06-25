"""
Low-level USB driver for the Thermaltake 3.9" Bar Type TFT LCD (264a:233d).

This is a refactor of the proven streaming engine from
github.com/bekindpleaserewind/ttlcd. The fragile init handshake and
packetisation are preserved verbatim; the only behavioural change is that
``Main`` now pulls each frame from a ``render_fn`` callback (returning a PIL
image) instead of rendering a static YAML-configured layout. That lets the
daemon stream live, animated content.

Single-device / single-process by design (uses module-level init flags, same
as upstream). Good enough for one panel on one machine.
"""
import math
import os
import struct
import threading
import time

import usb.core
import usb.util
from PIL import Image

# --- panel constants (do not change; wrong values can brick the panel) -------
RESOLUTION = (480, 128)
DPI = (300, 300)
IMAGE_PACKET_SIZE = 1020
IMAGE_CMD_SIZE = 4
DESIRED_CONFIG = 1

ROTATE_TOP, ROTATE_LEFT, ROTATE_BOTTOM, ROTATE_RIGHT = 0, 90, 180, 270
_ORIENT = {"top": ROTATE_TOP, "left": ROTATE_LEFT, "bottom": ROTATE_BOTTOM, "right": ROTATE_RIGHT}

# --- module-level init handshake state (mirrors upstream) --------------------
GLOBAL_INIT_LOCK = 0
MAX_GLOBAL_INIT = 13
GLOBAL_STAT = False
GLOBAL_RUNNING = False


def _reset_globals():
    global GLOBAL_INIT_LOCK, GLOBAL_STAT, GLOBAL_RUNNING
    GLOBAL_INIT_LOCK = 0
    GLOBAL_STAT = False
    GLOBAL_RUNNING = False


class USBControl:
    def __init__(self, dev, logger, endpoint=None):
        self.device = dev
        self.logger = logger
        self.endpoint = endpoint
        self.lock = threading.Lock()

    def read(self, buflen=-1, timeout=10000):
        try:
            self.lock.acquire()
            buf = usb.util.create_buffer(buflen) if buflen >= 0 else 0
            self.endpoint.read(buf, timeout)
            return buf
        except Exception:
            self.logger.warning("Failed to read endpoint %s", self.endpoint)
        finally:
            self.lock.release()

    def write(self, data=b""):
        try:
            self.lock.acquire()
            self.endpoint.write(data)
        except Exception:
            self.logger.warning("Failed to write endpoint %s", self.endpoint)
        finally:
            self.lock.release()

    def descriptor(self, index, language):
        return usb.util.get_string(self.device, index, language)

    def build(self, padding_len, *args):
        packstr = "B" * len(args)
        if padding_len > 0:
            padding = bytes("\x00" * padding_len, "utf-8")
            packstr += r"%ds"
            return struct.pack(packstr % (len(padding),), *args, padding)
        return struct.pack(packstr, *args)

    def raw_build(self, left_padding_len, right_padding_len, *args):
        packstr = "B" * len(args)
        left_padding, right_padding = [], []
        if left_padding_len > 0:
            left_padding = bytes("\x00" * left_padding_len, "utf-8")
            packstr = r"%ds" + packstr
        if right_padding_len > 0:
            right_padding = bytes("\x00" * right_padding_len, "utf-8")
            packstr = packstr + r"%ds"
        if left_padding_len > 0 and right_padding_len > 0:
            return struct.pack(packstr % (left_padding_len, right_padding_len), left_padding, *args, right_padding)
        if right_padding_len > 0:
            return struct.pack(packstr % (right_padding_len,), *args, right_padding)
        if left_padding_len > 0:
            return struct.pack(packstr % (left_padding_len,), left_padding, *args)
        return struct.pack(packstr, *args)


class Control(threading.Thread):
    def __init__(self, dev, logger):
        self.device = dev
        self.logger = logger
        self.running = False
        threading.Thread.__init__(self, daemon=True)

    def run(self):
        self.running = True
        self.control = USBControl(self.device, self.logger)
        try:
            self.init()
        except usb.core.USBError:
            self.logger.error("USBError during init: please reset the device")

    def shutdown(self):
        self.running = False

    def init(self):
        global GLOBAL_INIT_LOCK
        while self.running and GLOBAL_INIT_LOCK < MAX_GLOBAL_INIT:
            if 0 <= GLOBAL_INIT_LOCK < 1:
                for index in [0x02, 0x03, 0x02, 0x03, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02]:
                    for _ in range(3):  # part of the device init handshake; retry, never abort
                        try:
                            self.control.descriptor(index, 0x0409)
                            break
                        except Exception:
                            time.sleep(0.05)
                GLOBAL_INIT_LOCK += 1
            time.sleep(0.1)


class Write(threading.Thread):
    def __init__(self, dev, endpoint, logger):
        self.device = dev
        self.endpoint = endpoint
        self.logger = logger
        self.running = False
        threading.Thread.__init__(self, daemon=True)

    def run(self):
        global GLOBAL_INIT_LOCK, GLOBAL_RUNNING
        self.running = True
        self.control = USBControl(self.device, self.logger, self.endpoint)
        self.init()
        while self.running:
            if 13 <= GLOBAL_INIT_LOCK < 14 and GLOBAL_RUNNING:
                time.sleep(2)
                self.control.write(self.control.build(436, 0x82, 0x01, 0x00, 0x80))
            else:
                time.sleep(0.1)

    def shutdown(self):
        self.running = False

    def init(self):
        global GLOBAL_INIT_LOCK
        steps = {1: (0x85,), 3: (0x87,), 5: (0x85,), 7: (0x87,), 9: (0x84,), 11: (0x81,)}
        while self.running and GLOBAL_INIT_LOCK < MAX_GLOBAL_INIT:
            for gate, (cmd,) in steps.items():
                if gate <= GLOBAL_INIT_LOCK < gate + 1:
                    self.control.write(self.control.build(436, cmd, 0x01, 0x00, 0x80))
                    GLOBAL_INIT_LOCK += 1
            time.sleep(0.1)

    def write(self, packet):
        self.control.write(packet)


class Read(threading.Thread):
    def __init__(self, dev, endpoint, logger):
        self.device = dev
        self.endpoint = endpoint
        self.logger = logger
        self.running = False
        threading.Thread.__init__(self, daemon=True)

    def run(self):
        global GLOBAL_INIT_LOCK, GLOBAL_RUNNING
        self.running = True
        self.control = USBControl(self.device, self.logger, self.endpoint)
        self.init()
        while self.running:
            if 15 <= GLOBAL_INIT_LOCK < 16 and GLOBAL_RUNNING:
                time.sleep(2)
                self.control.read(440, 2000)
            else:
                time.sleep(0.1)

    def shutdown(self):
        self.running = False

    def init(self):
        # These are drain-reads; device init is driven by the Write commands, so
        # a short timeout keeps startup fast (the data is discarded either way).
        global GLOBAL_INIT_LOCK
        while self.running and GLOBAL_INIT_LOCK < MAX_GLOBAL_INIT:
            for gate in (2, 4, 6, 8, 10, 12):
                if gate <= GLOBAL_INIT_LOCK < gate + 1:
                    self.control.read(440, 3000)
                    GLOBAL_INIT_LOCK += 1
            time.sleep(0.1)


class Main(threading.Thread):
    """Streams frames produced by ``render_fn`` (a callable -> PIL.Image)."""

    def __init__(self, dev, endpoint, write_endpoint, render_fn, image_path, logger, orientation=0):
        self.device = dev
        self.endpoint = endpoint
        self.write_endpoint = write_endpoint
        self.render_fn = render_fn
        self.image_path = image_path
        self.logger = logger
        self.orientation = orientation
        self.running = False
        self.block = False
        threading.Thread.__init__(self, daemon=True)

    def _save_frame(self):
        img = self.render_fn()
        if img is None:
            img = Image.new("RGB", RESOLUTION, (0, 0, 0))
        if img.size != RESOLUTION:
            img = img.resize(RESOLUTION)
        if self.orientation:
            img = img.rotate(self.orientation)
        img.save(self.image_path, "JPEG", quality=80, optimize=False, dpi=DPI, progressive=False)

    def run(self):
        global GLOBAL_INIT_LOCK, GLOBAL_STAT
        self.running = True
        self.control = USBControl(self.device, self.logger, self.endpoint)
        first = 0
        while self.running:
            if 13 <= GLOBAL_INIT_LOCK < 14 and not GLOBAL_STAT:
                if first == 0:
                    self.write_endpoint.write(self.control.build(435, 0x12, 0x01, 0x00, 0x80, 0x64))
                    first = 1
                try:
                    self._save_frame()
                except Exception as e:
                    self.logger.warning("render failed: %s", e)
                    time.sleep(0.1)
                    continue
                self._stream(self.image_path)
                GLOBAL_STAT = True
            else:
                time.sleep(0.05)
        self.logger.info("Shutdown Main")

    def _stream(self, image_path):
        with open(image_path, "rb") as rd:
            bbytes = list(rd.read())
        iterations = math.ceil(len(bbytes) / IMAGE_PACKET_SIZE)
        index, start, pkt_index = 0, 0, 1
        while index < iterations:
            if index == 0:
                command = [0x08, iterations, 0x00, 0x80]
                if len(bbytes) > start + IMAGE_PACKET_SIZE:
                    data = command + bbytes[0:start + IMAGE_PACKET_SIZE]
                    packet = self.control.raw_build(0, 0, *data)
                else:
                    data = command + bbytes
                    packet = self.control.raw_build(0, IMAGE_PACKET_SIZE + IMAGE_CMD_SIZE - len(data), *data)
            else:
                pkt_command = [0x08, pkt_index, 0x00, 0x00]
                if len(bbytes[start:]) > IMAGE_PACKET_SIZE:
                    data = pkt_command + bbytes[start:start + IMAGE_PACKET_SIZE]
                    packet = self.control.raw_build(0, 0, *data)
                else:
                    data = pkt_command + bbytes[start:]
                    packet = self.control.raw_build(0, IMAGE_PACKET_SIZE + IMAGE_CMD_SIZE - len(data), *data)
                pkt_index += 1
            while self.block:
                time.sleep(0.25)
            self.control.write(packet)
            index += 1
            start += IMAGE_PACKET_SIZE

    def shutdown(self):
        self.running = False


class Trigger(threading.Thread):
    def __init__(self, dev, endpoint, logger):
        self.device = dev
        self.endpoint = endpoint
        self.logger = logger
        self.running = False
        threading.Thread.__init__(self, daemon=True)

    def run(self):
        global GLOBAL_STAT, GLOBAL_RUNNING
        self.running = True
        self.control = USBControl(self.device, self.logger, self.endpoint)
        while self.running:
            if 13 <= GLOBAL_INIT_LOCK < 14 and GLOBAL_STAT:
                self.control.read(16, 2000)
                GLOBAL_STAT = False
                GLOBAL_RUNNING = True
            time.sleep(0.05)

    def shutdown(self):
        self.running = False


class LcdDriver:
    """
    Owns the panel. Construct with a render callback, call ``start()`` to spin
    up the streaming threads (non-blocking), and ``stop()`` to tear down.

    render_fn: callable returning a 480x128 PIL.Image for the current frame.
    """

    VENDOR = 0x264A
    PRODUCT = 0x233D

    def __init__(self, render_fn, logger, image_path, orientation="top",
                 vendor=VENDOR, product=PRODUCT):
        self.render_fn = render_fn
        self.logger = logger
        self.image_path = image_path
        self.orientation = _ORIENT.get(str(orientation).lower(), ROTATE_TOP)
        self.vendor = vendor
        self.product = product
        self.dev = None
        self._threads = []

    @classmethod
    def device_present(cls, vendor=VENDOR, product=PRODUCT):
        try:
            return usb.core.find(idVendor=vendor, idProduct=product) is not None
        except Exception:
            return False

    def find_device(self):
        dev = usb.core.find(idVendor=self.vendor, idProduct=self.product)
        if dev is None:
            raise RuntimeError("Thermaltake LCD %04x:%04x not found" % (self.vendor, self.product))
        return dev

    def setup(self):
        _reset_globals()
        self.dev = self.find_device()
        try:
            cfg = self.dev.get_active_configuration()
        except usb.core.USBError:
            cfg = None
        if cfg is None or cfg.bConfigurationValue != DESIRED_CONFIG:
            self.dev.set_configuration(DESIRED_CONFIG)
            cfg = self.dev.get_active_configuration()
        for i in (0, 1):
            if self.dev.is_kernel_driver_active(i):
                try:
                    self.dev.detach_kernel_driver(i)
                except usb.core.USBError as e:
                    self.logger.error("Could not detach kernel driver %d: %s", i, e)
        i0 = cfg[(0, 0)]
        i1 = cfg[(1, 0)]
        self.endpoints = [i0[0], i0[1], i1[0], i1[1]]
        time.sleep(1.0)  # let the device settle after (re)configuration

    def start(self):
        if self.dev is None:
            self.setup()
        control = Control(self.dev, self.logger)
        write = Write(self.dev, self.endpoints[0], self.logger)
        read = Read(self.dev, self.endpoints[1], self.logger)
        main = Main(self.dev, self.endpoints[2], write, self.render_fn,
                    self.image_path, self.logger, self.orientation)
        trigger = Trigger(self.dev, self.endpoints[3], self.logger)
        self._threads = [control, write, read, main, trigger]
        for t in self._threads:
            t.start()
        self.logger.info("LCD driver running")

    def stop(self):
        # Signal, then JOIN every thread before returning so no stale thread is
        # alive to mutate the shared GLOBAL_* state when a new connect resets it.
        for t in self._threads:
            if hasattr(t, "shutdown"):
                t.shutdown()
        for t in self._threads:
            try:
                t.join(timeout=5.0)  # USB reads can block up to ~3s before exiting
            except Exception:
                pass
        self._threads = []
        if self.dev is not None:
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass

    def reset_usb(self):
        """Clear a wedged-but-present device via USBDEVFS_RESET on its /dev node.

        IMPORTANT: we do NOT use pyusb's ``dev.reset()`` — on this panel that call
        drops the device off the bus and it does not re-enumerate without a replug.
        The ioctl below resets the device while keeping it enumerated.
        """
        import fcntl
        import glob
        vid, pid = "%04x" % self.vendor, "%04x" % self.product
        for d in glob.glob("/sys/bus/usb/devices/*"):
            try:
                if (open(d + "/idVendor").read().strip() == vid
                        and open(d + "/idProduct").read().strip() == pid):
                    bus = int(open(d + "/busnum").read())
                    num = int(open(d + "/devnum").read())
                    node = "/dev/bus/usb/%03d/%03d" % (bus, num)
                    fd = os.open(node, os.O_WRONLY)
                    try:
                        fcntl.ioctl(fd, 0x5514, 0)  # USBDEVFS_RESET
                        return True
                    finally:
                        os.close(fd)
            except Exception as e:
                self.logger.warning("ioctl reset failed: %s", e)
        return False
