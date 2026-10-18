// Move only. Follow with a normal Computer Use click at the same screenshot pixel.
import AppKit
import ApplicationServices

func fail(_ message: String) -> Never {
    fputs("\(message)\n", stderr)
    exit(1)
}

let args = CommandLine.arguments.dropFirst().compactMap(Double.init)
guard CommandLine.arguments.count == 5, args.count == 4,
      args.allSatisfy({ $0.isFinite }), args[2] > 0, args[3] > 0,
      args[0] >= 0, args[0] < args[2], args[1] >= 0, args[1] < args[3] else {
    fail("Usage: swift scripts/isaac_client_move.swift x y screenshot_width screenshot_height")
}
let path = "/Applications/Isaac Sim WebRTC Streaming Client.app"
guard let client = NSWorkspace.shared.runningApplications.first(where: {
    $0.bundleURL?.path == path
}) else { fail("Open the installed streaming client first.") }
guard AXIsProcessTrusted() else { fail("Accessibility access is unavailable.") }
let windows = CGWindowListCopyWindowInfo(.optionAll, kCGNullWindowID) as? [[String: Any]] ?? []
guard let window = windows.first(where: {
    ($0[kCGWindowOwnerPID as String] as? Int) == Int(client.processIdentifier) &&
    ($0[kCGWindowLayer as String] as? Int) == 0 &&
    ($0[kCGWindowName as String] as? String) == "Isaac Sim WebRTC Streaming Client"
}), let dictionary = window[kCGWindowBounds as String] as? NSDictionary,
   let bounds = CGRect(dictionaryRepresentation: dictionary),
   bounds.width > 0, bounds.height > 0 else { fail("Client window not found.") }
let scaleX = args[2] / bounds.width
let scaleY = args[3] / bounds.height
guard abs(scaleX / scaleY - 1) < 0.01 else {
    fail("Window dimensions changed or screenshot was cropped. Capture again.")
}
let point = CGPoint(x: bounds.minX + args[0] / scaleX,
                    y: bounds.minY + args[1] / scaleY)
let element = AXUIElementCreateApplication(client.processIdentifier)
var mainWindow: CFTypeRef?
guard AXUIElementCopyAttributeValue(element, kAXMainWindowAttribute as CFString,
                                    &mainWindow) == .success,
      let mainWindow, CFGetTypeID(mainWindow) == AXUIElementGetTypeID() else {
    fail("Client main window unavailable; no mouse event sent.")
}
let windowElement = unsafeBitCast(mainWindow, to: AXUIElement.self)
guard AXUIElementPerformAction(windowElement, kAXRaiseAction as CFString) == .success,
      AXUIElementSetAttributeValue(windowElement, kAXMainAttribute as CFString,
                                   kCFBooleanTrue) == .success,
      AXUIElementSetAttributeValue(windowElement, kAXFocusedAttribute as CFString,
                                   kCFBooleanTrue) == .success else {
    fail("Cannot focus client main window; no mouse event sent.")
}
guard AXUIElementSetAttributeValue(element, kAXFrontmostAttribute as CFString,
                                  kCFBooleanTrue) == .success else {
    fail("Cannot activate client; no mouse event sent.")
}
RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.2))
guard NSWorkspace.shared.frontmostApplication?.processIdentifier == client.processIdentifier else {
    fail("Client did not acquire focus; no mouse event sent.")
}
guard let event = CGEvent(mouseEventSource: CGEventSource(stateID: .hidSystemState),
                          mouseType: .mouseMoved, mouseCursorPosition: point,
                          mouseButton: .left) else { fail("Cannot create mouse event.") }
event.flags = []
event.post(tap: .cghidEventTap)
RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.2))
print("Moved only: screenshot (\(args[0]), \(args[1])) -> screen points \(point); scale \(scaleX), \(scaleY). Verify the subsequent click in a fresh screenshot.")
