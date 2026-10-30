import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from camera import MicroscopeCamera


class CameraRecordingTests(unittest.TestCase):
    def test_default_time_lapse_accepts_disabled_free_space_threshold(self):
        camera = MicroscopeCamera()
        camera._source = object()
        frame = np.zeros((2, 2, 3), dtype=np.uint8)
        writer = Mock()
        writer.isOpened.return_value = True

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(camera, "get_frame", return_value=frame), \
                 patch("camera.cv2.VideoWriter", return_value=writer):
                camera.start_recording(
                    Path(tmp) / "video.mp4",
                    minimum_free_gb=None,
                )

                self.assertEqual(camera.recording_mode, "time_lapse")
                self.assertEqual(camera.recording_interval_seconds, 30.0)
                self.assertEqual(camera.recording_fps, 1.0)
                camera.stop_recording()


if __name__ == "__main__":
    unittest.main()