"""Shared headless Toupcam camera. Importing this package opens no device."""

from .camera import MicroscopeCamera
from .capture import ToupcamCapture

__all__ = ["MicroscopeCamera", "ToupcamCapture"]
