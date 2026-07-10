from machine import I2C
from lib.pcf8574 import PCF8574
from lib.hd44780 import HD44780
from lib.lcd import LCD

LCD_ADDR = 0x27

class Display():
    def __init__(self, bus: I2C):
        pcf = PCF8574(bus, address=LCD_ADDR)
        hd = HD44780(pcf, num_lines=2, num_columns=16)
        self.LCD = LCD(hd, pcf)
        self.LCD.backlight_on()

    def _format_lux(self, lux):
        if lux < 1_000:
            return f"{lux:.0f}"

        k_lux = lux / 1_000

        if lux < 100_000:
            return f"{k_lux:.1f}K"

        if lux < 1_000_000:
            return f"{k_lux:.0f}K"

        m_lux = lux / 1_000_000
        return f"{m_lux:.1f}M"

    def render(self, state):
        self.LCD.write_line(f"Lux:{self._format_lux(state.lux_seconds)}s|M:{state.moisture:.0f}%", 0)
        self.LCD.write_line(f"DLI:{state.dli}",1)
        pass
    
    def write(self, body):
        self.LCD.write_line(str(body), 0)

    def write_line(self, body, line: int):
        self.LCD.write_line(str(body), line)

    def display_err(self, desc: str, errno: int):
        self.LCD.write_line(f"Err[{str(errno)}]")
        self.LCD.marquee_text(desc, 1)

