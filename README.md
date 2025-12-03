# MatrixOS

A modular LED matrix display system with IPC-based sandboxed apps for Raspberry Pi.

## Architecture

MatrixOS is designed with a clean separation between the **core system** and **apps**:

```
src/matrix_os/
├── core/                    # System kernel and infrastructure
│   ├── kernel.py           # Main orchestrator, non-blocking render loop
│   ├── display.py          # Hardware abstraction layer
│   ├── ipc.py              # Message bus for app communication
│   ├── sandbox.py          # Thread/process isolation for apps
│   ├── scheduler.py        # App rotation and display scheduling
│   └── config.py           # System configuration
│
├── apps/                    # Sandboxed applications
│   ├── base.py             # Base app class with capability declarations
│   ├── clock.py            # Digital and binary clock displays
│   ├── dvd.py              # Bouncing DVD logo animation
│   ├── earth.py            # Earth with day/night terminator
│   ├── imageviewer.py      # Static image display
│   ├── slack.py            # Slack status display
│   ├── stocks.py           # Stock ticker with charts
│   ├── weather.py          # Weather display
│   └── welcome.py          # Boot animation
│
└── main.py                  # Entry point
```

## Key Design Principles

### 1. Non-blocking Render Loop
The main render loop in the kernel **never blocks**. All app logic runs in separate threads or processes.

### 2. IPC-based Communication
Apps communicate with the kernel through a message bus:
- Apps submit rendered frames via `FRAME_READY` messages
- Kernel sends lifecycle events (`APP_START`, `APP_STOP`, etc.)
- No direct hardware access from apps

### 3. Capability-based Sandboxing
Apps declare their required capabilities in a manifest:

```python
class MyApp(BaseApp):
    @classmethod
    def get_manifest(cls) -> AppManifest:
        return AppManifest(
            name="My App",
            framerate=30,
            capabilities={Capability.NETWORK},  # Requires network access
        )
```

Apps with `NETWORK` or `FILESYSTEM` capabilities automatically run in **separate processes** for isolation. Other apps run in lightweight threads.

### 4. Framebuffer Rendering
Apps never touch the hardware directly. Instead, they render to a `FrameBuffer`:

```python
def render(self) -> FrameBuffer:
    self.fb.clear()
    self.fb.set_pixel(10, 10, 255, 0, 0)  # Red pixel
    return self.fb
```

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/matrix-os.git
cd matrix-os

# Install with uv
uv sync

# Or with pip
pip install -e .
```

## Configuration

Create a `.env` file with your API keys:

```bash
# Slack
SLACK_USER_ID=your_user_id
SLACK_TOKEN=your_token

# Weather (OpenWeatherMap)
WEATHER_API_KEY=your_key
LAT=37.7749
LON=-122.4194

# Stocks (TwelveData)
STOCKS_API_KEY=your_key

# Timezone
LOCAL_TZ=America/Los_Angeles
```

## Usage

```bash
# Run directly
python src/matrix_os/main.py

# Or as a module
python -m matrix_os

# Or use the CLI entry point
matrix-os
```

## Creating Apps

To create a new app, inherit from `BaseApp`:

```python
from matrix_os.apps import BaseApp, AppManifest, Capability
from matrix_os.core import FrameBuffer

class MyApp(BaseApp):
    @classmethod
    def get_manifest(cls) -> AppManifest:
        return AppManifest(
            name="My App",
            version="1.0.0",
            description="A custom app",
            framerate=30,
            capabilities=set(),  # No special capabilities needed
        )
    
    def on_start(self) -> None:
        """Called when app starts."""
        self.counter = 0
    
    def update(self) -> None:
        """Update app state (called each frame)."""
        self.counter += 1
    
    def render(self) -> FrameBuffer:
        """Render current frame."""
        self.fb.clear()
        x = self.counter % self.width
        self.fb.set_pixel(x, 16, 255, 255, 255)
        return self.fb
    
    def on_stop(self) -> None:
        """Called when app stops."""
        pass
```

Then register it with the kernel:

```python
from matrix_os.core import Kernel
from my_app import MyApp

kernel = Kernel()
kernel.register_app(MyApp, duration=15)  # Display for 15 seconds
kernel.run()
```

## Hardware

This project is designed for LED matrix displays using the [rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) library. Default configuration:

- **Hardware mapping**: `adafruit-hat-pwm`
- **Matrix size**: 64x32 pixels
- **Chain length**: 1
- **Brightness**: 100%

Modify `core/config.py` to adjust hardware settings.

## License

MIT License
