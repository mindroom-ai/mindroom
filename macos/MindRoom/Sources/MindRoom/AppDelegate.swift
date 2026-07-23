import AppKit
import Foundation

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        StatusMenuController.shared.start()
        MindRoomCommandRunner.shared.refreshStatus()
    }

    func applicationWillTerminate(_ notification: Notification) {
        StatusMenuController.shared.stop()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}
