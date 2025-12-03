"""
System configuration for MatrixOS.
"""

from dataclasses import dataclass, field
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvSettings(BaseSettings):
    """Environment-based settings loaded from .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    stocks_api_key: str = ""
    slack_user_id: str = ""
    slack_token: str = ""
    local_tz: str = "America/Los_Angeles"
    weather_api_key: str = ""
    lat: float = 0.0
    lon: float = 0.0


@dataclass
class DisplayConfig:
    """Hardware display configuration."""

    hardware_mapping: str = "adafruit-hat-pwm"
    rows: int = 32
    cols: int = 64
    chain_length: int = 1
    parallel: int = 1
    row_address_type: int = 0
    multiplexing: int = 0
    pwm_bits: int = 11
    brightness: int = 100
    pwm_lsb_nanoseconds: int = 130
    led_rgb_sequence: str = "RGB"
    pixel_mapper_config: str = ""
    panel_type: str = ""
    show_refresh_rate: int = 0
    gpio_slowdown: int = 4
    disable_hardware_pulsing: bool = False
    drop_privileges: bool = True


@dataclass
class SchedulerConfig:
    """App scheduling configuration."""

    default_app_duration: int = 15  # seconds
    transition_duration: float = 0.5  # seconds
    max_framerate: int = 60
    min_framerate: int = 1


@dataclass
class SystemConfig:
    """Complete system configuration."""

    display: DisplayConfig = field(default_factory=DisplayConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    env: Optional[EnvSettings] = None

    def __post_init__(self):
        if self.env is None:
            try:
                self.env = EnvSettings()
            except Exception:
                self.env = EnvSettings.model_construct()
