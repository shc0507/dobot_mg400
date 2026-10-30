# Third-party notices

`thermal_cam/_vendor/p3_camera.py` is an unmodified copy of the driver from
[jvdillon/p3-ir-camera](https://github.com/jvdillon/p3-ir-camera), pinned to commit
`e3205dca5727682ff2d903585d1dce5a1d19f1f6`.

Upstream author: Joshua V. Dillon and contributors. USB protocol research is
attributed upstream to @aeternium. The upstream project is licensed under the
Apache License 2.0, reproduced in `thermal_cam/_vendor/LICENSE-p3-ir-camera`.

The application-specific `thermal_cam/camera.py` adapter adds Windows backend
selection, resource cleanup, bounded frame parsing, and P1 shutter handling.
The upstream project is independent of the camera manufacturer.
