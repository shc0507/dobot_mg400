#!/usr/bin/python3
"""Install the uvcvideo driver with the mjpeg_bpp patch through DKMS.

The stock driver lets an AC410 reserve its largest isochronous slot for every MJPEG
mode above 640x480. The patch estimates the slot from the frame size and rate instead,
so several cameras fit on one USB 2 link. The script downloads the upstream driver
source for the kernel series (for example v6.14), applies mjpeg-bpp.patch, builds it
with DKMS and sets `options uvcvideo mjpeg_bpp=200`. DKMS rebuilds the module for each
new kernel of the same series. Other kernel series keep the stock driver; run the
script again after you boot one.

If a step fails, the script removes its DKMS package and the stock driver stays.
The script does not reload the driver. Stop the camera and thermal units, then run
`rmmod uvcvideo` and `modprobe uvcvideo`, or reboot.

Usage: sudo /usr/bin/python3 deploy/uvcvideo/install.py [KERNEL_RELEASE]
"""

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

NAME = "uvcvideo-mjpeg"
BPP = 200
PATCH = Path(__file__).with_name("mjpeg-bpp.patch")
OPTIONS = Path("/etc/modprobe.d/mg400-uvcvideo.conf")
LISTING = "https://api.github.com/repos/torvalds/linux/contents/drivers/media/usb/uvc?ref=v{}"
DKMS_CONF = """\
PACKAGE_NAME="{name}"
PACKAGE_VERSION="{version}"
BUILT_MODULE_NAME[0]="uvcvideo"
DEST_MODULE_LOCATION[0]="/updates/dkms"
AUTOINSTALL="yes"
BUILD_EXCLUSIVE_KERNEL="^{series_re}\\."
"""


def run(*command):
    print("run:", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "mg400-uvcvideo"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def installed(version, kernel):
    status = subprocess.run(
        ["dkms", "status", "-m", NAME, "-v", version, "-k", kernel],
        capture_output=True,
        text=True,
        check=False,
    )
    return status.returncode == 0 and "installed" in status.stdout


def other_versions(version):
    status = subprocess.run(["dkms", "status", "-m", NAME], capture_output=True, text=True)
    found = re.findall(rf"^{re.escape(NAME)}/([^,\s]+)", status.stdout, re.MULTILINE)
    return sorted(set(found) - {version})


def build(series, version, kernel):
    source = Path(f"/usr/src/{NAME}-{version}")
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary) / "src"
        work.mkdir()
        for entry in json.loads(fetch(LISTING.format(series))):
            if entry.get("type") == "file" and re.fullmatch(r"[A-Za-z0-9_.-]+", entry["name"]):
                (work / entry["name"]).write_bytes(fetch(entry["download_url"]))
        run("patch", "-p1", "--forward", "-d", str(work), "-i", str(PATCH))
        (work / "dkms.conf").write_text(
            DKMS_CONF.format(name=NAME, version=version, series_re=series.replace(".", "\\."))
        )
        shutil.rmtree(source, ignore_errors=True)
        shutil.copytree(work, source)
    run("dkms", "add", "-m", NAME, "-v", version)
    run("dkms", "install", "-m", NAME, "-v", version, "-k", kernel)


def main(argv):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise SystemExit("Run this script as root on Linux.")
    kernel = argv[1] if len(argv) > 1 else platform.release()
    match = re.match(r"([0-9]+)\.([0-9]+)\.", kernel)
    if match is None:
        raise SystemExit(f"Unknown kernel release: {kernel}")
    series = f"{match[1]}.{match[2]}"
    version = f"{series}-{hashlib.sha256(PATCH.read_bytes()).hexdigest()[:8]}"
    if shutil.which("dkms") is None or not Path(f"/lib/modules/{kernel}/build").exists():
        raise SystemExit(f"Install dkms and linux-headers-{kernel}. The stock driver stays.")
    if installed(version, kernel):
        print(f"{NAME}/{version} is already installed for {kernel}.")
    else:
        for old in other_versions(version):
            run("dkms", "remove", "-m", NAME, "-v", old, "--all")
            shutil.rmtree(f"/usr/src/{NAME}-{old}", ignore_errors=True)
        try:
            build(series, version, kernel)
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            subprocess.run(["dkms", "remove", "-m", NAME, "-v", version, "--all"], check=False)
            shutil.rmtree(f"/usr/src/{NAME}-{version}", ignore_errors=True)
            raise SystemExit(
                f"The patched driver was not installed ({error}). The stock driver stays."
            )
    OPTIONS.write_text(
        f"# Written by deploy/uvcvideo/install.py.\noptions uvcvideo mjpeg_bpp={BPP}\n"
    )
    print(f"Wrote {OPTIONS}.")
    print(
        "Load the driver: mg400ctl stop camera thermal; rmmod uvcvideo; modprobe uvcvideo; "
        "mg400ctl start camera thermal. Or reboot."
    )


if __name__ == "__main__":
    main(sys.argv)
