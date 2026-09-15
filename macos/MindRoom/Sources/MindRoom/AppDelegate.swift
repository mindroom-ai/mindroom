import AppKit
import Foundation

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var waitingForDesktopHelperShutdown = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        StatusMenuController.shared.start()
        MindRoomCommandRunner.shared.refreshStatus()
        DesktopControlStore.shared.refresh()
    }

    func applicationWillTerminate(_ notification: Notification) {
        StatusMenuController.shared.stop()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if waitingForDesktopHelperShutdown {
            return .terminateLater
        }
        let needsDrain = DesktopControlStore.shared.shutdownHelper { [weak self] in
            self?.waitingForDesktopHelperShutdown = false
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        if needsDrain {
            waitingForDesktopHelperShutdown = true
            return .terminateLater
        }
        return .terminateNow
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}
