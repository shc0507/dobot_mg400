// Observe desktop state. This script does not send input or change windows.
import AppKit
import ApplicationServices
import Foundation

let usage = "Usage: swift scripts/isaac_client_monitor.swift duration_seconds [interval_ms]"
let arguments = Array(CommandLine.arguments.dropFirst())
if arguments == ["--help"] {
    print(usage)
    print("Duration: 0.1–300 seconds. Interval: 5–1000 ms. Default interval: 20 ms.")
    print("Output: JSON Lines with baseline, transition, and final records.")
    print("Accessibility reads require existing access. This script does not request access.")
    exit(0)
}
guard (1...2).contains(arguments.count),
      let duration = Double(arguments[0]), duration.isFinite,
      (0.1...300).contains(duration),
      let intervalMS = Double(arguments.count == 2 ? arguments[1] : "20"),
      intervalMS.isFinite, (5...1000).contains(intervalMS) else {
    fputs("\(usage)\n", stderr)
    exit(2)
}

struct ApplicationState: Codable, Equatable {
    let pid: pid_t
    let bundle_id: String?
}

struct PointerState: Codable, Equatable {
    let x: Double
    let y: Double
}

struct WindowState: Codable, Equatable {
    let id: UInt
    let focused: Bool?
    let minimized: Bool?
    let read_errors: [String: Int32]
}

struct ClientState: Codable, Equatable {
    let pids: [pid_t]
    let frontmost: Bool
    let accessibility_available: Bool
    let windows: [WindowState]
    let windows_error: Int32?
}

struct DesktopState: Codable, Equatable {
    let frontmost: ApplicationState?
    let pointer: PointerState
    let client: ClientState
}

let clientPath = "/Applications/Isaac Sim WebRTC Streaming Client.app"
var axPID: pid_t?
var axClient: AXUIElement?

func readBoolean(_ element: AXUIElement, _ attribute: String) -> (Bool?, Int32?) {
    var value: CFTypeRef?
    let result = AXUIElementCopyAttributeValue(element, attribute as CFString, &value)
    return (value as? Bool, result == .success ? nil : result.rawValue)
}

func capture() -> DesktopState {
    let frontmost = NSWorkspace.shared.frontmostApplication
    let pointer = NSEvent.mouseLocation
    let clients = NSWorkspace.shared.runningApplications.filter { $0.bundleURL?.path == clientPath }
    let pids = clients.map(\.processIdentifier).sorted()
    let trusted = AXIsProcessTrusted()
    var windows: [WindowState] = []
    var windowsError: Int32?
    if trusted, pids.count == 1 {
        if axPID != pids[0] {
            axPID = pids[0]
            axClient = AXUIElementCreateApplication(pids[0])
            // This timeout affects this observer's AX calls, not the client window.
            AXUIElementSetMessagingTimeout(axClient!, 0.02)
        }
        var value: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(axClient!, kAXWindowsAttribute as CFString, &value)
        if result == .success, let elements = value as? [AXUIElement] {
            windows = elements.map { window in
                AXUIElementSetMessagingTimeout(window, 0.02)
                let (focused, focusError) = readBoolean(window, kAXFocusedAttribute)
                let (minimized, minimizeError) = readBoolean(window, kAXMinimizedAttribute)
                var errors: [String: Int32] = [:]
                if let error = focusError { errors["focused"] = error }
                if let error = minimizeError { errors["minimized"] = error }
                return WindowState(id: CFHash(window), focused: focused,
                                   minimized: minimized, read_errors: errors)
            }.sorted { $0.id < $1.id }
        } else {
            windowsError = result == .success ? AXError.failure.rawValue : result.rawValue
        }
    }
    return DesktopState(
        frontmost: frontmost.map { ApplicationState(pid: $0.processIdentifier, bundle_id: $0.bundleIdentifier) },
        pointer: PointerState(x: pointer.x, y: pointer.y),
        client: ClientState(pids: pids, frontmost: frontmost.map { pids.contains($0.processIdentifier) } ?? false,
                            accessibility_available: trusted, windows: windows, windows_error: windowsError)
    )
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.sortedKeys]
func emit(_ kind: String, elapsed: Double, state: DesktopState, extra: [String: Any] = [:]) {
    do {
        let encoded = try encoder.encode(state)
        var record = extra
        record["kind"] = kind
        record["elapsed_ms"] = elapsed * 1000
        record["state"] = try JSONSerialization.jsonObject(with: encoded)
        let data = try JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
        FileHandle.standardOutput.write(data + Data([10]))
    } catch {
        fputs("Cannot encode monitor output: \(error)\n", stderr)
        exit(1)
    }
}

let started = ProcessInfo.processInfo.systemUptime
var previous: DesktopState?
var previousSampleTime = started
var samples = 0
var changes = ["frontmost": 0, "pointer": 0, "client": 0]
var clientFrontmostObserved = false
var clientMinimizedObserved = false
var maxGap = 0.0
var axUnavailableSamples = 0

while true {
    let state = capture()
    let now = ProcessInfo.processInfo.systemUptime
    let elapsed = now - started
    samples += 1
    if samples > 1 { maxGap = max(maxGap, now - previousSampleTime) }
    previousSampleTime = now
    clientFrontmostObserved = clientFrontmostObserved || state.client.frontmost
    clientMinimizedObserved = clientMinimizedObserved || state.client.windows.contains { $0.minimized == true }
    if !state.client.accessibility_available || state.client.pids.count != 1 ||
        state.client.windows_error != nil || state.client.windows.isEmpty ||
        state.client.windows.contains(where: { !$0.read_errors.isEmpty }) {
        axUnavailableSamples += 1
    }
    if let last = previous {
        var changed: [String] = []
        if last.frontmost != state.frontmost { changed.append("frontmost") }
        if last.pointer != state.pointer { changed.append("pointer") }
        if last.client != state.client { changed.append("client") }
        for field in changed { changes[field, default: 0] += 1 }
        if !changed.isEmpty { emit("transition", elapsed: elapsed, state: state, extra: ["changed": changed]) }
    } else {
        emit("baseline", elapsed: elapsed, state: state, extra: [
            "duration_seconds": duration, "interval_ms": intervalMS,
            "started_at": ISO8601DateFormatter().string(from: Date()),
            "client_path": clientPath, "pointer_coordinates": "AppKit screen points",
            "limitations": "Samples can miss changes between reads. Changes do not identify their cause."
        ])
    }
    previous = state
    if elapsed >= duration {
        emit("final", elapsed: elapsed, state: state, extra: [
            "samples": samples, "changes": changes, "max_sample_gap_ms": maxGap * 1000,
            "client_frontmost_observed": clientFrontmostObserved,
            "client_minimized_observed": clientMinimizedObserved,
            "client_ax_unavailable_samples": axUnavailableSamples
        ])
        break
    }
    RunLoop.current.run(until: Date(timeIntervalSinceNow: min(intervalMS / 1000, duration - elapsed)))
}
