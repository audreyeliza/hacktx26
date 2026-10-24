"""MMA8452Q on I2C0 (AGENTS.md §3).

WHO_AM_I at 0x0D is 0x2A. Address is 0x1C or 0x1D. ±2 g (XYZ_DATA_CFG 0x0E = 0x00),
then active (CTRL_REG1 0x2A). Samples are 12-bit left-justified, 1024 counts/g.
"""

REG_STATUS = 0x00
REG_OUT_X_MSB = 0x01
REG_WHO_AM_I = 0x0D
REG_XYZ_DATA_CFG = 0x0E
REG_CTRL_REG1 = 0x2A

WHO_AM_I = 0x2A
ADDRS = (0x1C, 0x1D)
COUNTS_PER_G = 1024


def raw_to_g(hi, lo):
    """One axis: two register bytes to g, rounded to 3 decimals, within ±2 g."""
    raw = ((hi & 0xFF) << 8) | (lo & 0xFF)
    if raw & 0x8000:
        raw -= 0x10000
    counts = raw >> 4
    g = counts / float(COUNTS_PER_G)
    if g > 2.0:
        g = 2.0
    elif g < -2.0:
        g = -2.0
    return round(g, 3)


class MMA8452Q:
    def __init__(self, i2c):
        self.i2c = i2c
        self.addr = self._find()
        # Range and the data rate can change only while the part is in standby.
        self.i2c.writeto_mem(self.addr, REG_CTRL_REG1, b"\x00")
        self.i2c.writeto_mem(self.addr, REG_XYZ_DATA_CFG, b"\x00")
        # 100 Hz ODR (faster than the 50 Hz sample loop) and active.
        self.i2c.writeto_mem(self.addr, REG_CTRL_REG1, b"\x19")

    def _find(self):
        for addr in ADDRS:
            try:
                who = self.i2c.readfrom_mem(addr, REG_WHO_AM_I, 1)
            except OSError:
                continue
            if who and who[0] == WHO_AM_I:
                return addr
        raise OSError("MMA8452Q not found")

    def read_g(self):
        raw = self.i2c.readfrom_mem(self.addr, REG_OUT_X_MSB, 6)
        return (
            raw_to_g(raw[0], raw[1]),
            raw_to_g(raw[2], raw[3]),
            raw_to_g(raw[4], raw[5]),
        )
