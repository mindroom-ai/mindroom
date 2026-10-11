import AppKit
import SwiftUI

@MainActor
final class AppWindowController: NSObject, NSWindowDelegate {
    static let shared = AppWindowController()
    let navigation = AppNavigation()
    private var window: NSWindow?

    func show(section: AppSection? = nil) {
        if let section { navigation.section = section }
        if window == nil {
            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 880, height: 680),
                styleMask: [.titled, .closable, .miniaturizable, .resizable],
                backing: .buffered, defer: false
            )
            window.title = "MindRoom"
            window.minSize = NSSize(width: 760, height: 580)
            window.isReleasedWhenClosed = false
            window.delegate = self
            window.contentView = NSHostingView(rootView: MindRoomRootView(
                navigation: navigation,
                runner: MindRoomCommandRunner.shared,
                desktop: DesktopControlStore.shared
            ))
            window.center()
            window.setFrameAutosaveName("MindRoomMainWindow")
            self.window = window
        }
        NSApp.setActivationPolicy(.regular)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func windowWillClose(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
    }
}
