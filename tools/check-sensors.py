# sensor_test.py - quick wiring check for the BreathWatch wearable (Pico 2W, MicroPython)
# Prints raw readings ~10x per second. Watch the numbers change as you breathe.
# Pins match AGENTS.md: GP26 pressure, GP27 pulse, GP28 thermistor, I2C0 GP4/GP5 accelerometer.
# Note: an analog pin with nothing connected "floats" and shows random noise - that's normal.

from machine import Pin, ADC, I2C
import time

pressure = ADC(Pin(26))
pulse = ADC(Pin(27))
therm = ADC(Pin(28))

i2c = I2C(0, sda=Pin(4), scl=Pin(5), freq=400000)
found = i2c.scan()
print("I2C devices found:", [hex(a) for a in found])

ACC = None
for addr in (0x1C, 0x1D):
    if addr in found:
        try:
            if i2c.readfrom_mem(addr, 0x0D, 1)[0] == 0x2A:  # WHO_AM_I
                ACC = addr
        except OSError:
            pass

if ACC is not None:
    i2c.writeto_mem(ACC, 0x2A, b"\x00")  # standby
    i2c.writeto_mem(ACC, 0x0E, b"\x00")  # +/-2 g range
    i2c.writeto_mem(ACC, 0x2A, b"\x01")  # active
    print("MMA8452Q found at", hex(ACC))
else:
    print("Accelerometer NOT found - check SDA/SCL/3V3/GND wiring")


def to_g(hi, lo):
    v = ((hi << 8) | lo) >> 4  # 12-bit, left-justified
    if v > 2047:
        v -= 4096
    return v / 1024


while True:
    line = "pressure={:5d}  pulse={:5d}  therm={:5d}".format(
        pressure.read_u16(), pulse.read_u16(), therm.read_u16())
    if ACC is not None:
        d = i2c.readfrom_mem(ACC, 0x01, 6)
        line += "  ax={:+.3f} ay={:+.3f} az={:+.3f}".format(
            to_g(d[0], d[1]), to_g(d[2], d[3]), to_g(d[4], d[5]))
    print(line)
    time.sleep(0.1)
