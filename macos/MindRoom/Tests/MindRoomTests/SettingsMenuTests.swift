import AppKit
import XCTest
@testable import MindRoom

@MainActor
final class SettingsMenuTests: XCTestCase {
    func testSettingsMenuRoutesToAppSettings() {
        let controller = StatusMenuController.shared
        let originalShowWindow = controller.showWindow
        defer { controller.showWindow = originalShowWindow }
        var selected: AppSection?
        controller.showWindow = { selected = $0 }

        XCTAssertTrue(NSApplication.shared.sendAction(
            NSSelectorFromString("openSettings"), to: controller, from: nil
        ))
        XCTAssertEqual(selected, .settings)
    }

    func testComputerAccessOpensAndReopensAppWithRemappedSettingsShortcut() {
        let application = NSApplication.shared
        let originalMenu = application.mainMenu
        let controller = StatusMenuController.shared
        let originalShowWindow = controller.showWindow
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
            controller.showWindow = originalShowWindow
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
        var selected: AppSection?
        controller.showWindow = { selected = $0; window.makeKeyAndOrderFront(nil) }

        for _ in 0 ..< 2 {
            XCTAssertFalse(window.isVisible)
            XCTAssertTrue(application.sendAction(
                NSSelectorFromString("openComputerAccess"),
                to: controller,
                from: nil
            ))
            XCTAssertEqual(selected, .computerAccess)
            XCTAssertTrue(window.isVisible, "Computer access must open the native app window")
            window.close()
        }
    }
}
