class Display():
    def __init__(self, LCD):
        self.LCD = LCD
        self.LCD.backlight_on()

    def _format_lux(self, lux):
        if lux < 1_000:
            return f"{lux:.0f}"

        k_lux = lux / 1_000

        if lux < 100_000:
            return f"{k_lux:.1f}K"

        return f"{k_lux:.0f}K"

    def render(self, state):
        self.LCD.write_line(f"Lux:{self._format_lux(state.lux_seconds)}s|M:{state.moisture:.0f}%", 0)
        self.LCD.write_line(f"DLI:{state.dli}",1)
        pass
    
    def write(self, body):
        self.LCD.write_line(str(body), 0)
