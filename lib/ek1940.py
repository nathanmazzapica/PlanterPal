from machine import Pin, ADC

class EK1940():
    def __init__(self, gpio: int):
        self.PIN = ADC(Pin(gpio))

    def moisture(self):
        """
            Returns the moisture as a u16
        """
        return self.PIN.read_u16()
