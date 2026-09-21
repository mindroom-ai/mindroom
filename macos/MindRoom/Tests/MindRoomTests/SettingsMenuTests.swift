import AppKit
import XCTest
@testable import MindRoom

@MainActor
final class SettingsMenuTests: XCTestCase {
    func testDesktopControlOpensAndReopensSettingsWithRemappedShortcut() {
        let application = NSApplication.shared
        let originalMenu = application.mainMenu
        let controller = StatusMenuController.shared
        let originalOpenSettings = controller.openSettingsAction
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 300, height: 200),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.isReleasedWhenClosed = false
        defer {
            window.close()
            application.mainMenu = originalMenu
            controller.openSettingsAction = originalOpenSettings
        }

        let mainMenu = NSMenu()
        let applicationItem = NSMenuItem()
        let applicationMenu = NSMenu()
        applicationItem.submenu = applicationMenu
        mainMenu.addItem(applicationItem)
        applicationMenu.addItem(NSMenuItem(title: "About", action: nil, keyEquivalent: ""))
        // Neither the localized title nor a customized shortcut should affect opening.
        let settingsItem = NSMenuItem(
            title: "Instellingen…",
            action: #selector(NSWindow.makeKeyAndOrderFront(_:)),
            keyEquivalent: "s"
        )
        settingsItem.target = window
        settingsItem.keyEquivalentModifierMask = [.command, .option]
        applicationMenu.addItem(settingsItem)
        application.mainMenu = mainMenu
        controller.openSettingsAction = { window.makeKeyAndOrderFront(nil) }

        for _ in 0 ..< 2 {
            XCTAssertFalse(window.isVisible)
            XCTAssertTrue(application.sendAction(
                NSSelectorFromString("openDesktopControl"),
                to: controller,
                from: nil
            ))
            XCTAssertTrue(window.isVisible, "Desktop Control must execute the registered settings action")
            window.close()
        }
    }
}
