from machine import I2C
from time import sleep

class BH1750():
    DEFAULT_ADDR = 0x23
    def __init__(self, i2c: I2C, addr=DEFAULT_ADDR):
        self.ADDR = addr
        self.MEASUREMENT_CONSTANT_S = 0.5
        self.MEASURE_HIRES_CMD = bytes([0b0010_0000])
        self.I2C = i2c

    def lux(self):
        self.I2C.writeto(self.ADDR, self.MEASURE_HIRES_CMD)
        sleep(self.MEASUREMENT_CONSTANT_S)

        data = self.I2C.readfrom(self.ADDR, 2)
        # BH1750 returns two bytes representing one 16 bit number.
        # First byte contains high bits, second low bits
        # Then, divide by 1.2 per the datasheet
        lux = (data[0] << 8 | data[1]) / 1.2
        return lux

        



