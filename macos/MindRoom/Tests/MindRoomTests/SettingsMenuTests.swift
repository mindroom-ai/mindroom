import AppKit
import Combine
import XCTest
@testable import MindRoom

@MainActor
final class SettingsMenuTests: XCTestCase {
    func testServiceControlsRequireCompletedSetup() async {
        let toggle = NSSelectorFromString("toggleLocalAgents")
        for (output, hasToggle) in [
            ("No such file", false), ("Service is not installed", false),
            ("Service: running", true), ("Service installed but not running", true),
        ] {
            let runner = MindRoomCommandRunner(processRunner: { _ in CommandResult(exitCode: 0, output: output) })
            let refreshed = expectation(description: "Service status refreshed")
            let subscription = runner.$serviceStatus.dropFirst().prefix(1).sink { _ in refreshed.fulfill() }
            runner.refreshStatus()
            await fulfillment(of: [refreshed], timeout: 3)
            let controller = StatusMenuController(runner: runner, desktop: DesktopControlStore())
            let menu = NSMenu()
            controller.menuNeedsUpdate(menu)

            XCTAssertEqual(menu.items.contains { $0.action == toggle }, hasToggle, output)
            XCTAssertEqual(menu.items.filter { $0.action == NSSelectorFromString("openLocalAgents") }.count, 1)
            withExtendedLifetime(subscription) {}
        }
    }

    func testStatusRowsOpenTheirOwnSections() throws {
        let controller = StatusMenuController.shared
        let originalShowWindow = controller.showWindow
        defer { controller.showWindow = originalShowWindow }
        let menu = NSMenu()
        controller.menuNeedsUpdate(menu)

        for (prefix, expected) in [("Local agents:", AppSection.localAgents), ("Computer access:", .computerAccess)] {
            let item = try XCTUnwrap(menu.items.first { $0.title.hasPrefix(prefix) })
            XCTAssertTrue(item.isEnabled, "Status must remain readable and actionable")
            var selected: AppSection?
            controller.showWindow = { selected = $0 }
            XCTAssertTrue(NSApplication.shared.sendAction(try XCTUnwrap(item.action), to: item.target, from: item))
            XCTAssertEqual(selected, expected)
        }
    }

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
