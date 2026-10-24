"""BreathWatch wearable firmware for the Pico 2W (AGENTS.md §3, §4, §13).

50 Hz polling loop timed with ticks_diff (no hardware timer). A packet of 25
samples goes out every 0.5 s: UDP to SERVER_IP:5005, or one JSON line on USB
serial if Wi-Fi is down. Bedside commands arrive on UDP 5006 and on stdin.
"""

import sys
import time

try:
    import select
except ImportError:
    select = None

DEVICE = "patch-01"
SAMPLE_MS = 20
BATCH = 25
UDP_PORT = 5005
CMD_PORT = 5006
LINK_LOST_MS = 10000
BLINK_MS = 250
LED_ON_MS = 40
WIFI_WAIT_MS = 8000
CATCHUP_MS = 100

# duty_u16. Orange is red with a dim green so it is not the same as yellow.
DUTY = 65535
ORANGE_G = 18000
COLOR = {
    "green": (0, DUTY, 0),
    "yellow": (DUTY, DUTY, 0),
    "orange": (DUTY, ORANGE_G, 0),
    "red": (DUTY, 0, 0),
    "blue": (0, 0, DUTY),
}


def encode_line(seq, t0_ms, pressure, pulse, therm, ax, ay, az):
    """Compact section-4 JSON. t0_ms is masked so a wrapped ticks_ms stays >= 0."""
    t0 = int(t0_ms) & 0x7FFFFFFF

    def ints(values):
        return "[" + ",".join(str(int(v)) for v in values) + "]"

    def gs(values):
        return "[" + ",".join("{:.3f}".format(float(v)) for v in values) + "]"

    return (
        '{"v":1,"device":"%s","seq":%d,"t0_ms":%d,"dt_ms":%d,'
        '"pressure":%s,"pulse":%s,"therm":%s,"ax":%s,"ay":%s,"az":%s}'
        % (DEVICE, int(seq), t0, SAMPLE_MS, ints(pressure), ints(pulse), ints(therm), gs(ax), gs(ay), gs(az))
    )


def read_counts(adc):
    try:
        return adc.read_u16()
    except Exception:
        return 0


def _adc(pin):
    import machine
    return machine.ADC(machine.Pin(pin))


def _pwm(pin):
    import machine
    out = machine.PWM(machine.Pin(pin))
    out.freq(1000)
    out.duty_u16(0)
    return out


def setup_wifi():
    """Return (udp_socket, server_ip) or (None, None). Sampling must still run."""
    try:
        import secrets
        ssid = secrets.WIFI_SSID
        password = secrets.WIFI_PASS
        server_ip = secrets.SERVER_IP
    except (ImportError, AttributeError):
        return None, None
    try:
        import network
        import socket
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        if not wlan.isconnected():
            wlan.connect(ssid, password)
            deadline = time.ticks_add(time.ticks_ms(), WIFI_WAIT_MS)
            while not wlan.isconnected() and time.ticks_diff(deadline, time.ticks_ms()) > 0:
                time.sleep_ms(100)
        if not wlan.isconnected():
            return None, None
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        return sock, server_ip
    except Exception:
        return None, None


def setup_cmd_socket():
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", CMD_PORT))
        sock.setblocking(False)
        return sock
    except Exception:
        return None


def setup_stdin():
    if select is None:
        return None
    try:
        poller = select.poll()
        poller.register(sys.stdin, select.POLLIN)
        return poller
    except Exception:
        return None


def apply_status(msg, bedside):
    if not isinstance(msg, dict) or msg.get("cmd") != "status":
        return
    color = msg.get("color")
    if color not in COLOR or color == "blue":
        return
    bedside["color"] = color
    bedside["buzzer"] = bool(msg.get("buzzer"))
    bedside["last"] = time.ticks_ms()


def take_command(text, bedside):
    text = text.strip()
    if not text:
        return
    try:
        import json
        msg = json.loads(text)
    except (ValueError, TypeError):
        return
    apply_status(msg, bedside)


def poll_commands(cmd_sock, poller, stdin_buf, bedside):
    if cmd_sock is not None:
        while True:
            try:
                data, _addr = cmd_sock.recvfrom(256)
            except OSError:
                break
            try:
                take_command(data.decode(), bedside)
            except Exception:
                pass
    if poller is None:
        return
    try:
        while poller.poll(0):
            ch = sys.stdin.read(1)
            if not ch:
                break
            if ch == "\n":
                take_command("".join(stdin_buf), bedside)
                stdin_buf[:] = []
            elif ch != "\r":
                stdin_buf.append(ch)
                if len(stdin_buf) > 240:
                    stdin_buf[:] = []
    except Exception:
        pass


def paint(rgb, buzzer, bedside, now, blink):
    lost = bedside["last"] is None or time.ticks_diff(now, bedside["last"]) > LINK_LOST_MS
    if lost:
        if blink["due"] is None or time.ticks_diff(now, blink["due"]) >= 0:
            blink["on"] = not blink["on"]
            blink["due"] = time.ticks_add(now, BLINK_MS)
        duty = COLOR["blue"] if blink["on"] else (0, 0, 0)
    else:
        blink["on"] = False
        blink["due"] = None
        duty = COLOR[bedside["color"]]
    buzzer.value(1 if bedside["buzzer"] else 0)
    for channel, level in zip(rgb, duty):
        channel.duty_u16(level)


def send_line(line, udp, server_ip, led, led_off):
    if led is not None:
        led.on()
        led_off["at"] = time.ticks_add(time.ticks_ms(), LED_ON_MS)
    sent = False
    if udp is not None and server_ip:
        try:
            udp.sendto(line.encode(), (server_ip, UDP_PORT))
            sent = True
        except OSError:
            sent = False
    if not sent:
        sys.stdout.write(line + "\n")
        try:
            sys.stdout.flush()
        except AttributeError:
            pass


def main():
    import machine
    from mma8452q import MMA8452Q

    pressure_adc = _adc(26)
    pulse_adc = _adc(27)
    therm_adc = _adc(28)
    try:
        i2c = machine.I2C(0, sda=machine.Pin(4), scl=machine.Pin(5), freq=400000)
        accel = MMA8452Q(i2c)
    except Exception:
        accel = None
    try:
        led = machine.Pin("LED", machine.Pin.OUT)
    except Exception:
        led = None
    rgb = (_pwm(16), _pwm(17), _pwm(18))
    buzzer = machine.Pin(15, machine.Pin.OUT)
    buzzer.off()

    udp, server_ip = setup_wifi()
    cmd_sock = setup_cmd_socket()
    poller = setup_stdin()

    bedside = {"color": "green", "buzzer": False, "last": None}
    blink = {"on": False, "due": None}
    led_off = {"at": None}
    stdin_buf = []

    pressure, pulse, therm = [], [], []
    ax, ay, az = [], [], []
    seq = 0
    t0 = 0
    next_sample = time.ticks_ms()

    if led is not None:
        led.off()

    while True:
        now = time.ticks_ms()
        if led is not None and led_off["at"] is not None and time.ticks_diff(now, led_off["at"]) >= 0:
            led.off()
            led_off["at"] = None
        paint(rgb, buzzer, bedside, now, blink)
        poll_commands(cmd_sock, poller, stdin_buf, bedside)

        if time.ticks_diff(now, next_sample) < 0:
            time.sleep_ms(1)
            continue
        if time.ticks_diff(now, next_sample) > CATCHUP_MS:
            next_sample = now

        if not pressure:
            t0 = now
        pressure.append(read_counts(pressure_adc))
        pulse.append(read_counts(pulse_adc))
        therm.append(read_counts(therm_adc))
        try:
            gx, gy, gz = accel.read_g() if accel is not None else (0.0, 0.0, 0.0)
        except Exception:
            gx, gy, gz = 0.0, 0.0, 0.0
        ax.append(gx)
        ay.append(gy)
        az.append(gz)
        next_sample = time.ticks_add(next_sample, SAMPLE_MS)

        if len(pressure) >= BATCH:
            line = encode_line(seq, t0, pressure, pulse, therm, ax, ay, az)
            send_line(line, udp, server_ip, led, led_off)
            seq += 1
            pressure, pulse, therm = [], [], []
            ax, ay, az = [], [], []


if __name__ == "__main__":
    main()
