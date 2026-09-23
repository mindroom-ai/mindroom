import AppKit
import Foundation

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var waitingForDesktopHelperShutdown = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        _ = AppUpdater.shared
        StatusMenuController.shared.start()
        MindRoomCommandRunner.shared.refreshStatus()
        DesktopControlStore.shared.refresh()
        if AppLaunchPolicy.shouldShowWindow(launchEvent: NSAppleEventManager.shared().currentAppleEvent) {
            AppWindowController.shared.show()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        StatusMenuController.shared.stop()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if MindRoomCommandRunner.shared.isRunningCommand {
            let alert = NSAlert()
            alert.messageText = "MindRoom Is Finishing an Action"
            alert.informativeText = "Wait for the current runtime action to finish before quitting. You can close the window while it runs."
            alert.addButton(withTitle: "Keep Running")
            alert.runModal()
            return .terminateCancel
        }
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

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        AppWindowController.shared.show()
        return true
    }
}
