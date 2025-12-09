# MatrixOS

A modular LED matrix display system with process-isolated apps for Raspberry Pi.

## Architecture

MatrixOS uses a clean separation between the **core system** and **apps**. Each app runs in its own process, completely isolated from the main render loop.

```
src/matrix_os/
├── core/                    # System kernel and infrastructure
│   ├── kernel.py           # Main orchestrator, non-blocking render loop
│   ├── display.py          # Hardware abstraction layer
│   ├── ipc.py              # Message bus for app communication
│   ├── sandbox.py          # Process isolation for apps
│   ├── scheduler.py        # App rotation and display scheduling
│   └── config.py           # System configuration
│
├── apps/                    # Process-isolated applications
│   ├── base.py             # Base app class
│   ├── fonts.py            # BDF font loading utilities
│   │
│   ├── clock/              # Clock apps
│   │   ├── __init__.py
│   │   └── app.py          # BasicClockApp, BinaryClockApp
│   │
│   ├── dvd/                # DVD bouncing logo
│   │   ├── __init__.py
│   │   └── app.py          # DVDApp
│   │
│   ├── earth/              # Earth day/night visualization
│   │   ├── __init__.py
│   │   └── app.py          # EarthApp
│   │
│   ├── imageviewer/        # Static image display
│   │   ├── __init__.py
│   │   └── app.py          # ImageViewerApp
│   │
│   ├── slack/              # Slack status display
│   │   ├── __init__.py
│   │   └── app.py          # SlackStatusApp
│   │
│   ├── stocks/             # Stock ticker with charts
│   │   ├── __init__.py
│   │   └── app.py          # StocksApp
│   │
│   └── weather/            # Weather display
│       ├── __init__.py
│       └── app.py          # WeatherApp
└── main.py                  # Entry point
```

## Key Design Principles

### 1. Process Isolation
Every app runs in its own **separate process**. This ensures:
- Network/API calls never block the display
- Crashed apps don't take down the system
- True isolation between apps

### 2. Non-blocking Render Loop
The main render loop in the kernel **never blocks**. Apps submit frames via IPC, and the kernel composites them to the display at a steady rate.

### 3. IPC-based Communication
Apps communicate with the kernel through a message bus:
- Apps submit rendered frames via `FRAME_READY` messages
- Kernel sends lifecycle events (`APP_START`, `APP_STOP`, etc.)
- No direct hardware access from apps

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

Each app lives in its own folder under `apps/`. Create a new folder with:
- `__init__.py` - exports the app class
- `app.py` - contains the app implementation

Example app structure:

```
apps/myapp/
├── __init__.py
└── app.py
```

**`apps/myapp/__init__.py`:**
```python
from .app import MyApp

__all__ = ["MyApp"]
```

**`apps/myapp/app.py`:**
```python
from ..base import AppManifest, BaseApp
from ...core.display import FrameBuffer

class MyApp(BaseApp):
    @classmethod
    def get_manifest(cls) -> AppManifest:
        return AppManifest(
            name="My App",
            version="1.0.0",
            description="A custom app",
            framerate=30,
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

Then register it with the kernel in `main.py`:

```python
from matrix_os.apps.myapp import MyApp

kernel.register_app(MyApp, duration=15)
```

## App Utilities

Apps have access to several utility methods:

```python
# Load fonts (BDF format)
from ..fonts import get_font

font_path = self.get_font_path("5x6.bdf")
font = get_font(font_path)

# Load images
image_path = self.get_image_path("icon.png")
image = self.load_image(image_path, size=(32, 32))

# Access environment settings
api_key = self.get_env("api_key", default="")
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
