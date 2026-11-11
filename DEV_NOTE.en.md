[中文](DEV_NOTE.md)

# Local dashboard development

The local preview uses the real Lab Console interface and Digital Twin routes with synthetic camera data. It requires no Olares connection or LarePass VPN.

Use this preview for appearance development. The shared dashboard header shows AI Chemistry Console and the logo from `src/orchestrator/web/static/console/console-logo.png`. The Digital Twin tab is labeled Position Finder. The workspace address remains `#twin`. Deploy appearance changes to Olares only when requested.

## Start and stop

Open PowerShell. Start the preview with the existing Python environment:

```powershell
Set-Location C:\Users\Swift\Documents\Github\dobot_mg400
.\.venv\Scripts\python.exe scripts\console_preview.py
```

Open [the local Digital Twin dashboard](http://127.0.0.1:8790/lab/#twin). Keep the terminal open. Press `Ctrl+C` in that terminal to stop the preview. Closing the browser does not stop the server.

If the environment or required packages are missing, install the preview and test dependencies from the repository root:

```powershell
uv sync --locked --no-default-groups --group vision --group dev
```

Then run the startup command again. Dependency installation requires network access when packages are not cached. The preview itself works offline.

If port `8790` is occupied, use another port:

```powershell
.\.venv\Scripts\python.exe scripts\console_preview.py --port 8791
```

Open `http://127.0.0.1:8791/lab/#twin` for that instance. The preview binds to `127.0.0.1`, so open it on the same PC. Use this launcher on Windows. The full orchestrator demo requires Linux.

## Edit and inspect

- Edit dashboard styles and scripts under `src/orchestrator/web/static/console/`. Refresh the browser after CSS or JavaScript changes. Use `Ctrl+F5` if it shows an older version.
- Restart the preview after Python or HTML template changes. Templates reside under `src/orchestrator/web/templates/`. The launcher does not reload Python or templates automatically.
- Use the Digital Twin monitor and calibration views to inspect layout, camera selection, validation messages, and profile workflows.
- Check appearance changes in both themes with the **Dark mode** / **Light mode** button beside the language switch. The browser remembers the choice for each Console address.
- Prioritize English labels. Use existing fonts or system fallbacks for translations. Font rebuilding is unnecessary for local preview work.

## Profiles and mock data

- Profiles and camera selections persist under `work/console-preview/calibration/twin/`. Git ignores `work/`. Local profiles do not synchronize with Olares.
- When that directory is absent, startup creates a `Mock stage` profile. Use it to inspect monitor readings and profile saving, loading, uploading, and deletion.
- Every demo camera returns the same fixed `640 × 480` marker image. Switching cameras does not select a physical camera. Readings do not simulate stage motion.
- A fixed image cannot supply distinct calibration endpoints. Repeated low and high captures cannot establish a new axis calibration.
- An uploaded profile can pass format validation but still have incompatible image dimensions. A profile for `3840 × 2160` images cannot measure the `640 × 480` mock frame.
- For a clean local profile store, stop the preview. Rename `work/console-preview/calibration/twin/` to an unused backup name. Restart the preview to create `Mock stage` again. Keep the backup if you need its profiles. Deleting profiles through the UI does not recreate the initial profile.

## Run local checks

After a preview or Digital Twin Python change, run the relevant tests from the repository root:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_console_preview.py tests/test_console_twin.py tests/test_stage_twin.py
.\.venv\Scripts\ruff.exe check
.\.venv\Scripts\ruff.exe format --check
```

For Digital Twin JavaScript changes, use Node.js available on `PATH`:

```powershell
node --test tests/test_console_twin.mjs
```

Inspect the changed view in the browser as well. Automated tests do not establish that the layout is correct. Run tests after code updates. Reuse those results when committing. Push and publish the branch manually.

## What still requires Olares

Other device panels report unavailable or offline in this preview. Device commands and stop buttons do not control physical equipment.

Test real camera resolution, capture timing, stream interruptions, calibration accuracy, and device commands on Olares. Successful mock checks do not verify those behaviors. See the [dashboard guide](docs/dashboard.en.md#run-offline) for the Linux demo.
