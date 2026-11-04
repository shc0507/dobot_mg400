[中文](olares.md)

# Olares configuration and maintenance

[Project home](../README.en.md) · [Network](#network) · [Get the code](#get-the-code) · [Migration](#migrate-from-the-old-layout) · [USB gripper adapter](#usb-gripper-adapter) · [Cameras](#cameras)

This page covers settings and troubleshooting specific to Olares.
See [Host deployment](deploy.en.md) for installation, updates, configuration, and daily commands.
Run the commands on this page as root in the Olares host terminal (Control Hub → Terminal → Olares).

## Network

Clients access Olares through the LarePass VPN. The Olares VPN address is `100.64.0.1`.
For direct client access through separate service ports, the robot, stage, and camera services must listen on this address. Set these values in `/etc/mg400/mg400.env`:

```ini
MG400_PANEL_BIND=100.64.0.1
MG400_CAMERA_BIND=100.64.0.1
MG400_STAGE_BIND=100.64.0.1
```

The Olares VPN access rules must allow TCP `8766`, `8767`, and `1985`.
The Olares network interface connected directly to the robot uses `192.168.1.10/24`. The robot address is `192.168.1.6`.

For the optional thermal service, set `MG400_THERMAL_BIND=100.64.0.1` and permit TCP `8768` through the VPN for direct access.
The default remains `127.0.0.1`. Keep `MG400_THERMAL_TOKEN` private and send it in the bearer header, never the URL.
Add `thermal` to `MG400_OPTIONAL_SERVICES` before installation. See the [thermal service guide](thermal.en.md#managed-host-service).

If the optional furnace service is enabled, set `MG400_FURNACE_BIND=100.64.0.1` and permit TCP `8769` through the VPN.
Keep `MG400_FURNACE_TOKEN` private and send it in the bearer header, never the URL. See the [furnace service](furnace.en.md).

The optional microscope service defaults to `MG400_MICROSCOPE_BIND=127.0.0.1` and port `8770`.
For direct VPN access, set `MG400_MICROSCOPE_BIND=100.64.0.1` and permit TCP `8770` through the VPN.
Keep `MG400_MICROSCOPE_TOKEN` private and send it in the bearer header, never the URL.
The token must contain at least 24 printable ASCII characters.
Add `microscope` to `MG400_OPTIONAL_SERVICES` before installation. See the [microscope guide](microscope.en.md#managed-host-service) for physical configuration and startup order.

The optional vision service defaults to `MG400_VISION_BIND=127.0.0.1` and port `8771`.
For direct VPN access, set `MG400_VISION_BIND=100.64.0.1` and permit TCP `8771` through the VPN.

Keep `MG400_VISION_TOKEN` private and send it in the bearer header, never the URL.
For non-loopback access, the token must have at least 24 printable ASCII characters. A configured token must meet this rule on loopback too.

Add `vision` to `MG400_OPTIONAL_SERVICES` before installation. See [Host deployment](deploy.en.md#service-registry) for the isolated runtime and required weights.
The owner installs and starts the service, then runs `mg400ctl gpu` to read GPU memory as UID `1000`.

If the optional recipe service must accept VPN clients, set `MG400_ORCH_BIND=100.64.0.1` and permit TCP `8772` through the VPN.
Send `MG400_ORCH_TOKEN` in the bearer header, never in the URL. Keep downstream token aliases private on the host.
The default bind remains `127.0.0.1`. See the [recipe service](orchestrator.en.md#http-service) for access and recovery rules.

Keep the optional [MCP gateway](agent-gateway.en.md) at `MG400_GATEWAY_BIND=127.0.0.1`, port `8773`. Do not open that port directly through the VPN.
Remote MCP clients require an authenticated tunnel to loopback and the `MG400_GATEWAY_TOKEN` bearer credential.

## Optional single-port proxy

The optional `proxy` service provides 1 VPN port for the Lab Console and the camera streams.
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

The proxy has only these routes:

| VPN path | Loopback upstream | Required native prefix |
| --- | --- | --- |
| `/lab/` | `127.0.0.1:8772` | `/lab/` |
| `/camera/<key>/<stream>.mjpeg`, `/camera/<key>/<stream>.jpg` | `127.0.0.1:1985` | `/<key>/` after removing only `/camera` |

The proxy redirects the root path `/` to the Lab Console at `/lab/`. See the [Lab Console](dashboard.en.md#lab-console).
The camera routes forward only video streams and single frames. The Cameras workspace of the console uses them.
The proxy does not forward the robot, stage, thermal, furnace, or microscope services.
These services listen on loopback ports with their native prefixes. The console, the command-line tools, and the recipe runner call them directly on `127.0.0.1`.
The camera port remains `1985`. Other upstream ports follow their `MG400_*_PORT` settings when supplied.
Selecting `proxy` does not select `thermal`. Add `thermal` separately to `MG400_OPTIONAL_SERVICES` to install its service.
A route and the console cannot work until the upstream service exists and runs with the required native prefix.
Keep a separately started service on `127.0.0.1` with its own token, even though its connection is local.
For thermal, set `MG400_THERMAL_BIND=127.0.0.1` and `MG400_THERMAL_BASE_PATH=/thermal/` in `/etc/mg400/mg400.env`.
The installer fills a blank `MG400_THERMAL_TOKEN` and preserves a configured token.
`thermal-cam --base-path /thermal/` overrides that prefix setting.

For the microscope with the proxy, set `MG400_MICROSCOPE_BIND=127.0.0.1` and `MG400_MICROSCOPE_BASE_PATH=/microscope/` in `/etc/mg400/mg400.env`.
When `MG400_MICROSCOPE_TOKEN` is blank, the installer generates a token. It preserves an existing token.
Select `microscope` separately from `proxy`. Update its furnace metadata URL when the furnace native prefix changes.
Do not place service tokens in Caddy configuration or URLs.
The MCP gateway remains loopback-only and has no proxy route.

Stop active work before changing service URLs. Update downstream client URLs to include the new native prefixes.
For example, use `MG400_ORCH_ROBOT_URL=http://127.0.0.1:8766/robot/` and `MG400_ORCH_STAGE_URL=http://127.0.0.1:8767/stage/contract/`.
The stage contract and operator routes are at `/stage/contract/api/`.
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
Open `http://100.64.0.1:8080/`. The proxy redirects it to the Lab Console.
After an update, the installer can reuse the installed binary without `--caddy`.

The proxy passes browser bearer credentials unchanged. It never supplies an upstream token.
Each service keeps its token, Host, Origin, lease, and command checks.
The proxy preserves the browser `Host`, including its port, and sets `X-Forwarded-Host` from that value.
It replaces incoming forwarded address headers and removes `Forwarded` and `X-Real-IP`.

Caddy forwards WebSocket `Upgrade` and `Connection` through its native reverse proxy, using HTTP/1.1 upstream.
Upstream response-header and read timeouts are `60` s.
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
   The old script uses the old lock directory. It cannot prevent concurrent control with the new services:

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

5. Copy the camera key and mode switch backups:

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

8. Enable the [Lab Console](dashboard.en.md#lab-console), then check the following:
   - The Cameras workspace shows all configured camera views.
   - The Motion workspace shows the robot state.
   - After you take control of the robot in the Motion workspace, `mg400ctl status` shows the gripper collector as `active`.
   - The stage card in the Motion workspace can connect to the Arduino.

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
The 4 AC410 cameras report the same serial number, `HU12321178`, so their stable names use port paths.
The camera on `12.1.4` is the microscope side camera. It watches the objective turret. go2rtc owns it, and the microscope reads its shared stream.
The camera on `12.1.2` sees the stage from above. The camera on `12.1.3` looks along the Insert rail, with the tray behind it.
The camera on `12.2.1` sees the tray. Add its path as `camera4` to show it in the console.

| Device | VID:PID | Stable identity | Hub port | Service |
| --- | --- | --- | --- | --- |
| Stage Arduino Uno R3 | `2341:0043` | `/dev/serial/by-id/usb-Arduino__www.arduino.cc__0043_1453230313235111D1E0-if00` | `12.2.3` | `stage` (`MG400_STAGE_SERIAL`) |
| Turret and focus Arduino Uno R3 | `2341:0043` | `/dev/serial/by-id/usb-Arduino__www.arduino.cc__0043_035353433353512070D1-if00` | `12.2.2` | `microscope` |
| Furnace FT231X | `0403:6015` | `/dev/serial/by-id/usb-FTDI_FT231X_USB_UART_DU0DT9G8-if00-port0` | `12.3.2` | `furnace` (`MG400_FURNACE_SERIAL`) |
| Gas controller FT232R | `0403:6001` | `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_ABBCE4JW-if00-port0` | `12.3.1` | `furnace` (`MG400_MFC_SERIAL`) |
| Gripper converter FT232R | `0403:6001` | `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_BG02Q5TN-if00-port0` | `12.3.3` | `collector` |
| Thermal Master P1 | `3474:45c2` | libusb, serial `P1016021DG040100208` | `12.1.1` | `thermal` |
| AC410 stage camera, from above | `f326:1306` | `/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.1.2:1.0-video-index0` | `12.1.2` | `camera` |
| AC410 Insert rail camera | `f326:1306` | `/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.1.3:1.0-video-index0` | `12.1.3` | `camera` |
| AC410 microscope side camera | `f326:1306` | `/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.1.4:1.0-video-index0` | `12.1.4` | `camera`, shared through go2rtc |
| AC410 tray camera | `f326:1306` | `/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.2.1:1.0-video-index0` | `12.2.1` | `camera` |
| ToupTek USB 3 camera | `0547:1375` (USB 3), `0547:1376` (USB 2) | Toupcam SDK, no serial number | `3.2.4` | `microscope`, shared with furnace recordings |

Use the full serial paths for controllers. The gas controller and gripper converter share a VID:PID, but have different serial numbers.
The thermal and Toupcam cameras use USB device nodes under `/dev/bus/usb`, without serial-port or video-device nodes.
The bus and device numbers can change. Let libusb or the SDK find the camera.

### USB access rules

The installer selects `config/udev/70-mg400-<name>.rules.in` only for registered, enabled services.
It replaces `@SERVICE_GROUP@` with the group name for GID `1000`, from the host group database.
Each rule matches a VID:PID and grants `MODE="0660"` to that group.
Serial rules also match the recorded device serial number. Camera rules preserve the existing port-based names.
The thermal rule matches the recorded P1 serial number as a prefix. The P1 adds non-printing bytes after its serial number, and udev compares the whole value.
The templates create no new device aliases and run no reset helper.

The installer writes `/etc/udev/rules.d/70-mg400-<name>.rules` as root with mode `0644`.
It then runs `udevadm control --reload-rules` and `udevadm trigger --subsystem-match=usb`.
The USB trigger does not reset a hub or start a service.
TTY and video permissions apply on their next udev event. Existing service access through `dialout` and `video` remains available.
Use the installer dry run to inspect the selected files and commands before installation.

The default services select the `stage`, `collector`, and `camera` templates.
Enabling `furnace` also selects its 2 serial-controller rules.
Enabling `thermal` selects the P1 template. Enabling `microscope` requires and selects its template.
The microscope owns the Toupcam camera, including furnace recordings. The furnace rules do not grant separate access to it.
Templates do not select device paths in service settings. Keep those paths consistent with the table.

## USB gripper adapter

The adapter's USB identity is `0403:6001`, and its serial number is `BG02Q5TN`.
Its stable host path is `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_BG02Q5TN-if00-port0`.
The `ttyUSB` number can change. The collector selects the device by its full USB identity.
Do not substitute another serial port on the dock.
The stage Arduino also connects to Olares USB. It is a separate entry in `/dev/serial/by-id/`.
The gripper collector does not use the Arduino. The stage service does not list the gripper adapter.

The collector reads 3 status registers every 50 ms and atomically replaces the feedback file `/var/lib/mg400/work/gripper_feedback.json`.
At startup, the collector service only reads status. It runs initialization or position commands only after a request from the robot service.
The collector keeps running after you close the console.
The collector exits if the serial port is busy, the device is missing, or a read fails.
After an abnormal exit, the service waits 2 seconds before restarting. Retries have no limit.
Collection resumes automatically when the USB connection returns. You do not need to open the console again.
Each start uses a new session. It does not replay old commands or clear the command lock.
Control can remain locked if the connection fails during a command. You must first check the gripper state.
The robot service and collector must run on the same host and share the same monotonic clock domain.
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

Configure 1 to 4 AC410 cameras with distinct USB port paths in `MG400_CAMERA_DEVICES`.
Stream names follow device order, from `camera1` through `camera4`. Empty settings disable cameras.
Set `MG400_CAMERA_MODES` to one comma-separated MJPEG mode per device. Only these modes are accepted:

```text
1920x1080@60, 1920x1080@30, 4000x3000@15, 3840x2160@30,
2592x1944@30, 2560x1440@30, 2048x1536@30, 1600x1200@30,
1280x720@30, 1024x768@30, 1024x576@30, 800x600@30,
640x480@30, 640x360@30, 320x240@30
```

The legacy `MG400_CAMERA_CAPTURE_SIZES` list supplies `WIDTHxHEIGHT` values at 30 fps.
Do not set both lists. With both lists empty, ordinary streams use `640x480@30` and `MG400_MICROSCOPE_SIDE_STREAM` uses `3840x2160@30`.
An explicit list overrides these defaults, including the side stream.
The following 4-camera configuration uses the port order in the table above and runs every camera at `640x480@30`.
This is the tested setting on Olares (2026-11-03). With the tray at `1280x720@30`, the shared USB 2 link truncated the frames of `camera2`.
Before you raise a mode, check the bandwidth with `mg400ctl camera-check` and the kernel log:

```ini
MG400_CAMERA_DEVICES=/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.1.2:1.0-video-index0,/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.1.3:1.0-video-index0,/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.1.4:1.0-video-index0,/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.2.1:1.0-video-index0
MG400_CAMERA_MODES=640x480@30,640x480@30,640x480@30,640x480@30
MG400_CAMERA_CAPTURE_SIZES=
MG400_MICROSCOPE_SIDE_STREAM=camera3
```

Only streams with width at most 640 and height at most 480 are preloaded.
Larger streams start when a video stream, microscope, or still consumer requests them.
After the last viewer leaves a large stream, the preview service releases its upstream reader between body reads.
Socket operations retain their 2-second timeout; this is not a total shutdown deadline. Other consumers can keep the stream active.
The video streams are capped at 30 fps, including when capture uses `1920x1080@60`.
Single-frame requests use the same go2rtc stream, at its configured resolution; they do not switch mode or open another device capture.

The 4 AC410 cameras and thermal camera share a 480 Mbit/s USB 2 link. Large streams can exceed its periodic transfer budget together.
H-21 requires supervised bandwidth checks before simultaneous hardware use.
The tray's approximately 30 mm Micro QR labels need at least 70 pixels across 17 modules.
At a 1280-pixel image width, a 556 mm tray field provides about 2.3 pixels/mm.
Keep each camera on its listed USB port. After changing devices or modes, run the installer again and restart the cameras.

Start the camera services:

```sh
mg400ctl start camera
```

Use the Cameras workspace of the Lab Console to see the views.
If the `proxy` service is enabled, the stream prefix is `http://<MG400_PROXY_BIND>:<MG400_PROXY_PORT>/camera/<key>/`.
The stream paths contain the access key. The key stays the same after a service restart.
Closing the terminal does not stop the preview service.
Stopping the preview service also stops the camera capture service.
If startup fails, run `mg400ctl logs camera`.

The server keeps only the latest pending frame. It discards queued frames when the network slows.
The server send timeout is 1.5 seconds.

### Camera failures

If an image looks wrong, first run `mg400ctl camera-check`.
It checks configured cameras by their USB port paths, then checks the 2 services and the expected go2rtc preloaded streams.
It reports larger streams as demand-started and does not require or start a continuous preload for them.
It exits with code 0 if all checks pass. If a check fails, it exits with code 1 and shows the next action.
If a camera is missing, it does not check the services.

If all streams stop together, first use `lsusb -t` and `ls -l /dev/video*` to check that the host still recognizes the devices.
An `active` service status only means that the process is alive.
When an image request returns `200`, also check that the image is not empty and can be decoded.
If the host recognizes neither the hub nor the cameras, a preview service restart cannot replace a USB connection check.

If the host recognizes the cameras but the stream frame rate is very low, run
`curl -s http://127.0.0.1:1984/api/preload` on the Olares host.
Check that the output includes each configured stream whose width is at most 640 and height is at most 480.
If an expected preload is missing, run `mg400ctl restart camera`, wait 2 seconds, and query again.
Preloading might not start if a device is missing when the capture service starts.
The console video streams reconnect automatically. The configured `camera<N>.jpg` single-frame endpoints can be slow when a stream starts on demand.

After increasing the resolution, check all required views together under H-21. Check that several consecutive JPEG images decode completely.
The resolution and frame rate shown by the browser do not rule out corrupted images or partial image damage.
If these problems occur, restore the `640 × 480` configuration, then check the USB capture path.

Every lab device shares the external hub. Never reset the shared upstream hub to recover a camera.
Check the affected camera's port and logs. Follow the supervised hardware procedure before changing a device connection.
A restored USB connection does not authorize robot, stage, gripper, furnace, or gas commands.
