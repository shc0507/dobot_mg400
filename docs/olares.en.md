[中文](olares.md)

# Olares configuration and maintenance

[Project home](../README.en.md) · [Network](#network) · [Get the code](#get-the-code) · [Migration](#migrate-from-the-old-layout) · [USB gripper adapter](#usb-gripper-adapter) · [Cameras](#cameras)

This page covers settings and troubleshooting specific to Olares.
See [Host deployment](deploy.en.md) for installation, updates, configuration, and daily commands.
Run the commands on this page as root in the Olares host terminal (Control Hub → Terminal → Olares).

## Network

Clients access Olares through the LarePass VPN. The Olares VPN address is `100.64.0.1`.
With separate service ports, the panels and camera preview must listen on this address. Set these values in `/etc/mg400/mg400.env`:

```ini
MG400_PANEL_BIND=100.64.0.1
MG400_CAMERA_BIND=100.64.0.1
MG400_STAGE_BIND=100.64.0.1
```

The Olares VPN access rules must allow TCP `8766`, `8767`, and `1985`.
The Olares network interface connected directly to the robot uses `192.168.1.10/24`. The robot address is `192.168.1.6`.

If the optional furnace service is enabled, set `MG400_FURNACE_BIND=100.64.0.1` and permit TCP `8769` through the VPN.
Keep `MG400_FURNACE_TOKEN` private. Enter it on the [furnace page](furnace.en.md), never in its URL.

If the optional recipe service must accept VPN clients, set `MG400_ORCH_BIND=100.64.0.1` and permit TCP `8772` through the VPN.
Send `MG400_ORCH_TOKEN` in the bearer header, never in the URL. Keep downstream token aliases private on the host.
The default bind remains `127.0.0.1`. See the [recipe service](orchestrator.en.md#http-service) for access and recovery rules.

Keep the optional [MCP gateway](agent-gateway.en.md) at `MG400_GATEWAY_BIND=127.0.0.1`, port `8773`. Do not open that port directly through the VPN.
Remote MCP clients require an authenticated tunnel to loopback and the `MG400_GATEWAY_TOKEN` bearer credential.

## Optional single-port proxy

The optional `proxy` service provides 1 VPN port for the instrument services.
It uses a Caddy 2 binary supplied through `--caddy PATH`.
The installer writes `/etc/mg400/Caddyfile` and installs `/usr/local/lib/mg400/caddy`.
It never downloads Caddy or starts the proxy.

Add `proxy` to `MG400_OPTIONAL_SERVICES`. Preserve other selected service names.
For a host with no other optional services, set these values in `/etc/mg400/mg400.env`:

```ini
MG400_OPTIONAL_SERVICES=proxy
MG400_PROXY_BIND=100.64.0.1
MG400_PROXY_PORT=8080
MG400_PANEL_BIND=127.0.0.1
MG400_PANEL_BASE_PATH=/robot/
MG400_STAGE_BIND=127.0.0.1
MG400_STAGE_BASE_PATH=/stage/
MG400_CAMERA_BIND=127.0.0.1
```

The proxy address must be 1 numeric loopback or VPN address, without a wildcard.
The proxy port must be from `1024` to `65535` and differ from every upstream port.
Upstream ports must also be distinct.
The installer checks enabled services' loopback addresses, native prefixes, and tokens before it writes files.

For enabled `furnace` or `orch` services, also set their corresponding values:

```ini
MG400_FURNACE_BIND=127.0.0.1
MG400_FURNACE_BASE_PATH=/furnace/
MG400_ORCH_BIND=127.0.0.1
MG400_ORCH_BASE_PATH=/lab/
```

The routes preserve each service's native prefix:

| VPN path | Loopback upstream | Required native prefix |
| --- | --- | --- |
| `/robot/` | `127.0.0.1:8766` | `/robot/` |
| `/stage/` | `127.0.0.1:8767` | `/stage/` |
| `/camera/<key>/` | `127.0.0.1:1985` | `/<key>/` after removing only `/camera` |
| `/thermal/` | `127.0.0.1:8768` | `/thermal/` |
| `/furnace/` | `127.0.0.1:8769` | `/furnace/` |
| `/microscope/` | `127.0.0.1:8770` | `/microscope/` |
| `/lab/` | `127.0.0.1:8772` | `/lab/` |

The camera port remains `1985`. Other upstream ports follow their `MG400_*_PORT` settings when supplied.
The proxy does not install or enable thermal or microscope services.
A route cannot work until its upstream service exists and runs with the required native prefix.
Keep a separately started service on `127.0.0.1` with its own token, even though its connection is local.
For thermal, set `MG400_THERMAL_TOKEN` and `MG400_THERMAL_BASE_PATH=/thermal/` in its startup environment.
`thermal-cam --base-path /thermal/` overrides that prefix setting.

For a separately installed microscope service, use `MG400_MICROSCOPE_TOKEN` and its `/microscope/` native prefix option.
These direct thermal and microscope tokens are not added to the shared environment example.
Configure their startup environments separately. Do not place tokens in Caddy configuration or URLs.
The MCP gateway remains loopback-only and has no proxy route.

Stop active work before changing service URLs. Update downstream client URLs to include the new native prefixes.
For example, use `MG400_ORCH_ROBOT_URL=http://127.0.0.1:8766/robot/` and `MG400_ORCH_STAGE_URL=http://127.0.0.1:8767/stage/contract/`.
The stage contract stays separate from its legacy page API at `/stage/contract/api/`.
Stage and thermal keep `/` as the default when no base path is configured.

After configuration, install the supplied binary and check the generated configuration:

```sh
/usr/bin/python3 /opt/mg400/deploy/install.py --dry-run --caddy /path/to/caddy
/usr/bin/python3 /opt/mg400/deploy/install.py --caddy /path/to/caddy
/usr/local/lib/mg400/caddy validate --config /etc/mg400/Caddyfile --adapter caddyfile
```

Restart the upstream services whose settings changed through `mg400ctl`.
Then start the proxy explicitly with `mg400ctl start proxy`.
Permit TCP `8080` through the VPN, or the configured proxy port.
Open `http://100.64.0.1:8080/robot/` or another route from the table.
For cameras, retain the complete secret key from the existing preview URL.
After an update, the installer can reuse the installed binary without `--caddy`.

The proxy passes browser bearer credentials unchanged. It never supplies an upstream token.
Each service keeps its token, Host, Origin, lease, and command checks.
The proxy preserves the browser `Host`, including its port, and sets `X-Forwarded-Host` from that value.
It replaces incoming forwarded address headers and removes `Forwarded` and `X-Real-IP`.

Caddy forwards WebSocket `Upgrade` and `Connection` through its native reverse proxy, using HTTP/1.1 upstream.
Upstream response-header and read timeouts are `60` s, above the robot's `5` s ping interval.
The proxy adds no request retry. It disables its admin API and configuration persistence.
The configuration excludes request access and error logs because camera URLs contain a secret.
This HTTP entry uses the VPN connection. It does not enable public HTTPS.

## Get the code

The repository is public. Olares reads the code from GitHub through HTTPS without credentials.
Follow [Host deployment](deploy.en.md#installation-and-updates) to get the code and run the installer.
If `/opt/mg400` has no `origin`, add it first:

```sh
git -C /opt/mg400 remote add origin https://github.com/shc0507/dobot_mg400.git
```

Olares has no GitHub write credentials and cannot push. Change the code locally, then push it to GitHub.

### When GitHub is unavailable

If Olares cannot access GitHub, transfer the code through a Git bundle:

1. Read the deployed commit on the Olares host:

   ```sh
   git -C /opt/mg400 rev-parse HEAD
   ```

2. Create a bundle in the local project root. Include only the history after that commit and record the target commit.
   Replace `HOST_SHA` with the commit from step 1:

   ```sh
   git bundle create /tmp/mg400-deploy.bundle main ^HOST_SHA
   git rev-parse main
   ```

   If `/opt/mg400` does not exist, use `--all` instead of `main ^HOST_SHA` to include the full history.

3. In Files on the Olares web interface, upload the bundle to `Home/Downloads` in Drive.
   This directory has the following path on the host:

   ```sh
   BUNDLE=/olares/rootfs/userspace/pvc-userspace-qumusphusis-8vsdwupyazk45sjv/Home/Downloads/mg400-deploy.bundle
   ```

4. If `/opt/mg400` does not exist, first run `git init -q /opt/mg400`.
   Replace `COMMIT_SHA` below with the target commit recorded in step 2:

   ```sh
   git -C /opt/mg400 bundle verify "$BUNDLE"
   git -C /opt/mg400 fetch "$BUNDLE" 'refs/heads/*:refs/remotes/bundle/*'
   git -C /opt/mg400 switch --detach COMMIT_SHA
   git -C /opt/mg400 rev-parse HEAD
   ```

   If `bundle verify` reports missing commits, create the bundle again with `--all`.

Then run the installer as described in [Host deployment](deploy.en.md#installation-and-updates).

## Migrate from the old layout

The old layout stores code and data in several directories. Some are in the IsaacLab application's storage.
Below, `$DOCS` means `/olares/userdata/Cache/pvc-appcache-qumusphusis-aah2phqnttncsxoh/isaaclab/documents`.

| Old location | Contents | New location |
| --- | --- | --- |
| `/opt/mg400-web-control` | Web panel code, with `MG400_PANEL_TOKEN` in `.env` | `/opt/mg400`, `/etc/mg400/mg400.env` |
| `$DOCS/mg400-isaac` | Gripper collector code | `/opt/mg400` |
| `$DOCS/mg400-terminal`, `$DOCS/mg400-tools` | Interactive terminal code, startup scripts, and mode switch tools | `mg400ctl terminal`, `mg400ctl mode` |
| `$DOCS/mg400-data` | Connection locks, gripper feedback, and mode switch backups | `/var/lib/mg400` |
| `/opt/mg400-camera-preview` | go2rtc, capture configuration, preview program, and keys | `/usr/local/lib/mg400`, `/etc/mg400`, `/var/lib/mg400/camera` |

Do this migration only once. Before migration, close all panel pages and interactive terminals, then disable the robot.

1. Stop the old services, copy the old service definitions as a backup, and disable the old terminal startup script.
   The old script uses the old lock directory. It cannot prevent concurrent control with the new panel:

   ```sh
   DOCS=/olares/userdata/Cache/pvc-appcache-qumusphusis-aah2phqnttncsxoh/isaaclab/documents
   systemctl stop mg400-web-panel mg400-stage-panel mg400-camera-preview mg400-gripper-collector.path mg400-gripper-collector
   mkdir -p /root/mg400-old-units
   cp -a /etc/systemd/system/mg400-* /root/mg400-old-units/
   mv "$DOCS/mg400-tools/start-terminal.sh" "$DOCS/mg400-tools/start-terminal.sh.disabled"
   ```

2. Follow [Get the code](#get-the-code) to switch `/opt/mg400` to the target commit.
3. Write the old web panel access key and camera devices to the configuration file. Users can keep their existing key:

   ```sh
   grep '^MG400_PANEL_TOKEN=' /opt/mg400-web-control/.env >> /etc/mg400/mg400.env
   grep -o '/dev/v4l/by-path/[^&]*' /opt/mg400-camera-preview/go2rtc.yaml | paste -sd, | sed 's/^/MG400_CAMERA_DEVICES=/' >> /etc/mg400/mg400.env
   ```

4. Run the installer and reuse the existing go2rtc:

   ```sh
   /usr/bin/python3 /opt/mg400/deploy/install.py --go2rtc /opt/mg400-camera-preview/go2rtc
   ```

5. Copy the camera preview key and mode switch backups. The preview link stays the same if you keep the original key:

   ```sh
   install -o 1000 -g 1000 -m 0600 /opt/mg400-camera-preview/preview.key /var/lib/mg400/camera/preview.key
   test -d "$DOCS/mg400-data/work/control-mode" && cp -a "$DOCS/mg400-data/work/control-mode" /var/lib/mg400/work/ && chown -R 1000:1000 /var/lib/mg400/work/control-mode
   ```

   Check for the gripper command lock. If this file exists, a gripper command has an unconfirmed outcome:

   ```sh
   ls -l "$DOCS/mg400-data/work/gripper_olares_feedback.json.command-lock.json"
   ```

   If the file exists, first check the gripper state. Then copy the lock under the name for the new feedback file.
   The new collector service keeps control locked when it reads this lock, as the old service does:

   ```sh
   install -o 1000 -g 1000 -m 0600 "$DOCS/mg400-data/work/gripper_olares_feedback.json.command-lock.json" /var/lib/mg400/work/gripper_feedback.json.command-lock.json
   ```

6. Set the bind addresses as described in [Network](#network).
7. Remove the old service override files and boot autostart links, then start the services.
   Step 1 copied these files as a backup:

   ```sh
   rm -rf /etc/systemd/system/mg400-web-panel.service.d
   rm -f /etc/systemd/system/*.wants/mg400-*.service
   systemctl daemon-reload
   systemctl is-enabled mg400-web-panel mg400-stage-panel mg400-camera-preview
   mg400ctl start web stage camera
   mg400ctl status
   ```

   If `is-enabled` shows `static` for all 3 services, they will not start at boot.

8. Check the following:
   - The link from `mg400ctl camera-url` opens all 3 camera views.
   - The web panel accepts the original access key.
   - After you click `Connect & take control` in the web panel, `mg400ctl status` shows the gripper collector as `active`.
   - The stage panel can connect to the Arduino.

Keep the old directories until you complete these checks.
To restore the old layout, first run `mg400ctl stop all`.
Then copy the files from `/root/mg400-old-units` to `/etc/systemd/system/` and run `systemctl daemon-reload`.
Rename `start-terminal.sh.disabled` to its original name to restore the old terminal startup script.

## USB device layout

Every lab USB device connects through 1 self-powered hub on 1 host port.
The controller is `0000:80:14.0`, with USB 2 port `12` and USB 3 port `3`.
Keep the Toupcam camera on the hub's USB 3 side. Never reset the shared hub.
A reset disconnects the motor, furnace, gas, gripper, and camera connections together.

Keep each camera on its listed hub port.
The 3 AC410 cameras report the same serial number, `HU12321178`, so their stable names use port paths.
The camera on `12.1.3` is the microscope side camera. go2rtc owns it, and the microscope reads its shared stream.
Confirm the views on `12.1.2` and `12.1.4` in the camera preview before assigning their roles.

| Device | VID:PID | Stable identity | Hub port | Service |
| --- | --- | --- | --- | --- |
| Stage Arduino Uno R3 | `2341:0043` | `/dev/serial/by-id/usb-Arduino__www.arduino.cc__0043_1453230313235111D1E0-if00` | `12.2.3` | `stage` (`MG400_STAGE_SERIAL`) |
| Turret and focus Arduino Uno R3 | `2341:0043` | `/dev/serial/by-id/usb-Arduino__www.arduino.cc__0043_035353433353512070D1-if00` | `12.2.2` | `microscope` |
| Furnace FT231X | `0403:6015` | `/dev/serial/by-id/usb-FTDI_FT231X_USB_UART_DU0DT9G8-if00-port0` | `12.3.2` | `furnace` (`MG400_FURNACE_SERIAL`) |
| Gas controller FT232R | `0403:6001` | `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_ABBCE4JW-if00-port0` | `12.3.1` | `furnace` (`MG400_MFC_SERIAL`) |
| Gripper converter FT232R | `0403:6001` | `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_BG02Q5TN-if00-port0` | `12.3.3` | `collector` |
| Thermal Master P1 | `3474:45c2` | libusb, serial `P1016021DG040100208` | `12.1.1` | `thermal` |
| AC410 camera | `f326:1306` | `/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.1.2:1.0-video-index0` | `12.1.2` | `camera` |
| AC410 microscope side camera | `f326:1306` | `/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.1.3:1.0-video-index0` | `12.1.3` | `camera`, shared through go2rtc |
| AC410 camera | `f326:1306` | `/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.1.4:1.0-video-index0` | `12.1.4` | `camera` |
| ToupTek USB 3 camera | `0547:1375` | Toupcam SDK, no serial number | `3.2.4` | `microscope`, shared with furnace recordings |

Use the full serial paths for controllers. The gas controller and gripper converter share a VID:PID, but have different serial numbers.
The thermal and Toupcam cameras use USB device nodes under `/dev/bus/usb`, without serial-port or video-device nodes.
The bus and device numbers can change. Let libusb or the SDK find the camera.

### USB access rules

The installer selects `config/udev/70-mg400-<name>.rules.in` only for registered, enabled services.
It replaces `@SERVICE_GROUP@` with the group name for GID `1000`, from the host group database.
Each rule matches a VID:PID and grants `MODE="0660"` to that group.
Serial rules also match the recorded device serial number. Camera rules preserve the existing port-based names.
The templates create no new device aliases and run no reset helper.

The installer writes `/etc/udev/rules.d/70-mg400-<name>.rules` as root with mode `0644`.
It then runs `udevadm control --reload-rules` and `udevadm trigger --subsystem-match=usb`.
The USB trigger does not reset a hub or start a service.
TTY and video permissions apply on their next udev event. Existing service access through `dialout` and `video` remains available.
Use the installer dry run to inspect the selected files and commands before installation.

The default services select the `stage`, `collector`, and `camera` templates.
Enabling `furnace` also selects its 2 serial-controller rules.
The `thermal` and `microscope` templates wait for their registered services to be enabled.
The microscope owns the Toupcam camera, including furnace recordings. The furnace rules do not grant separate access to it.
Templates do not select device paths in service settings. Keep those paths consistent with the table.

## USB gripper adapter

The adapter's USB identity is `0403:6001`, and its serial number is `BG02Q5TN`.
Its stable host path is `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_BG02Q5TN-if00-port0`.
The `ttyUSB` number can change. The collector selects the device by its full USB identity.
Do not substitute another serial port on the dock.
The stage Arduino also connects to Olares USB. It is a separate entry in `/dev/serial/by-id/`.
The gripper collector does not use the Arduino. The stage panel does not list the gripper adapter.

The collector reads 3 status registers every 50 ms and atomically replaces the feedback file `/var/lib/mg400/work/gripper_feedback.json`.
At startup, the collector service only reads status. It runs initialization or position commands only after a panel request.
The collector keeps running after you close the panel.
The collector exits if the serial port is busy, the device is missing, or a read fails.
After an abnormal exit, the service waits 2 seconds before restarting. Retries have no limit.
Collection resumes automatically when the USB connection returns. You do not need to open the panel again.
Each start uses a new session. It does not replay old commands or clear the command lock.
Control can remain locked if the connection fails during a command. You must first check the gripper state.
The panel and collector must run on the same host and share the same monotonic clock domain.
See [USB gripper protocol](development.en.md#usb-gripper-protocol) for the command file and lock formats.
Read the logs with `mg400ctl logs collector`.

### Restore the USB connection

If the logs report that `BG02Q5TN` is missing, first check USB on the host:

```sh
ls -l /dev/serial/by-id/
dmesg --ctime | grep -E 'BG02Q5TN|ftdi_sio|ttyUSB|device descriptor|accepting address'
```

`device descriptor read` or `device not accepting address` indicates a USB enumeration failure.
First use the port path in the logs to identify the original adapter, then restore that USB connection.
Replacing the hub cable does not change the adapter serial number. Do not select another serial port because of a cable change.
If the kernel exposes a `disable` file for that port, you can enumerate that port again on its own.
First stop the collector watcher and collector service.
Write `1` to `disable`, then write `0` and restore the watcher.
Do not reset the shared upstream hub. This would disconnect other devices.
After you check that `BG02Q5TN` appears again, start the collector and check that the feedback file updates continuously.

If the service or watcher is in the `failed` state, restore the USB connection and then run:

```sh
systemctl reset-failed mg400-gripper-collector.service mg400-gripper-collector.path
systemctl restart mg400-gripper-collector.path
mg400ctl start collector
```

## Cameras

3 AC410 cameras connect to Olares USB. Their configuration uses USB port paths.
The preview resolution is `640 × 480`. Each camera uses 1 continuous MJPEG stream at up to 30 frames per second.
Keep each camera on its original USB port.
After changing a port, update `MG400_CAMERA_DEVICES`, then run the installer again and restart the cameras.

Start the preview, then show the link that contains the access key:

```sh
mg400ctl start camera
mg400ctl camera-url
```

Connect to the LarePass VPN, then open the link in a browser.
The link contains the access key and stays the same after a service restart.
Closing the terminal does not stop the preview service.
Stopping the preview also stops the camera capture service.
If startup fails, run `mg400ctl logs camera`.

Below each image, the page shows the decoded dimensions and update rate. Camera, network, and browser performance affect the actual frame rate.
The server and browser keep only the latest pending frame. They discard queued frames when the network slows.
The server send timeout is 1.5 seconds.
If the page receives no complete frame for 1.5 seconds, it reconnects and temporarily keeps the previous frame.
The page shows “Image paused · Not live” when the stream stops or the visible frame reaches 1.5 seconds since receipt.
The page keeps the previous frame for at most 5 seconds after receipt. Reconnection does not extend this limit.
Switching to another browser tab pauses the preview and clears the image. Returning to the preview tab resumes it.

### Detection overlays

Enable `识别叠加` on a camera tile to show detection boxes, outlines, class names, and confidence.
The switch is off when the page opens.
Chip uses green `#18b85a`. The robotic arm uses orange `#ed8a24`.
The colors follow class names, regardless of the model's numeric class IDs.

Each visible tile with a live image requests detections at most once per second.
Enabled cameras take turns within the page. Slow requests can reduce the update rate.
Scrolling a tile out of view, hiding the browser tab, disabling its switch, or pausing its image cancels and clears its overlay.
These actions preserve the existing video pause rules.
Without a new result, the overlay expires after 1.5 seconds. Browser detection requests time out after 2 seconds.

Detection uses a separate frame and can lag behind the video.
The overlay uses image pixels and accounts for blank margins around the image.
It hides results whose image dimensions differ from the current frame.
Do not use the overlay as a calibrated robot motion target.

The access-key route `/<key>/camera<N>/detections.json` calls the fixed endpoint `http://127.0.0.1:8771/api/detect?camera=N`.
The vision service must use port `8771` and base path `/` for this proxy.
The preview reads `MG400_VISION_TOKEN` and sends it as a Bearer header. The browser never receives this token.
The preview unit retains this variable from `/etc/mg400/mg400.env`.
The proxy uses a 2-second socket timeout, bypasses environment proxies, and refuses redirects.
It rejects invalid JSON and detection responses larger than 256 KiB.

On any vision error, the route returns `{"available":false}` and the tile shows `识别不可用`.
Video continues independently of vision.
The preview does not start the vision service. See [Vision service](vision.en.md) for its runtime and startup requirements.

### Camera failures

If an image looks wrong, first run `mg400ctl camera-check`.
It checks the 3 cameras by their configured USB port paths, then checks the 2 services and go2rtc preloaded streams.
It exits with code 0 if all checks pass. If a check fails, it exits with code 1 and shows the next action.
If a camera is missing, it does not check the services.

If all 3 streams stop together, first use `lsusb -t` and `ls -l /dev/video*` to check that the host still recognizes the devices.
An `active` service status only means that the process is alive.
When an image request returns `200`, also check that the image is not empty and can be decoded.
If the host recognizes neither the hub nor the cameras, a preview service restart cannot replace a USB connection check.

If the host recognizes the cameras but the preview frame rate is very low, run
`curl -s http://127.0.0.1:1984/api/preload` on the Olares host.
Check that the output includes `camera1`, `camera2`, and `camera3`.
If any are missing, run `mg400ctl restart camera`, wait 2 seconds, and query again.
Preloading might not start if a device is missing when the capture service starts.
The web video streams reconnect automatically. The retained single-frame endpoints `camera1.jpg`, `camera2.jpg`, and `camera3.jpg` can be slow without continuous capture.

After increasing the resolution, check all 3 views together. Check that several consecutive JPEG images decode completely.
The resolution and frame rate shown by the browser do not rule out corrupted images or partial image damage.
If these problems occur, restore the `640 × 480` configuration, then check the USB capture path.

Every lab device shares the external hub. Never reset the shared upstream hub to recover a camera.
Check the affected camera's port and logs. Follow the supervised hardware procedure before changing a device connection.
A restored USB connection does not authorize robot, stage, gripper, furnace, or gas commands.
