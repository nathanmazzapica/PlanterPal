# PlanterPal

PlanterPal is an IoT project that started when I built a planter for my succulent then thought:

> "Hey I wonder exactly how much sun this guy is getting throughout the day"

With that thought I set off to teach myself some basics of using microcontrollers and sensors. From there
I've just kept following my curiosity. I went from just wanting a simple on-board csv logging system to
building a small backend the device could send updates too. The next steps include a tiny mobile app, and designing
a PCB and a functional, weather-proof enclosure so that I can actually keep this thing outside 24/7 in my planter.

As of writing this I have 0 experience with CAD, 3D printing, or PCB design so it might be a little while but I'll get there.

Anything for my succulent

## Project Setup

### Components
This project uses a standard ESP32 board, a BH1750 Lux sensor, a EK1940 capacitive soil moisture sensor,
and a 2x16 character LCD with an I2C backpack.

### Pins
The I2C SCL pin is 27, and the SDA pin is 26. The EK1940 uses pin 32 for analog input.

### Libraries
This project uses [Thomascountz's HD44780 LCD Controller Interface](https://github.com/Thomascountz/micropython_i2c_lcd)

### Config
`web/wifi_config.example.py` provides an example web config file. I intend to move this to a global config file, but not yet.

## Architecture

Notes on the architecture can be found in the docs/ directory
