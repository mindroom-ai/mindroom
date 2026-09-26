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

    func testPendingShellCommandOffersReviewThatOpensComputerAccessWithoutApproving() async throws {
        var actions: [String] = []
        let helper = DesktopBridgeProcess()
        let desktop = DesktopControlStore(helper: helper, request: { action, _, _ in
            actions.append(action)
            return [:]
        })
        let request = DesktopShellRequest(
            requestID: "shell-1", requesterID: "@person:example.org", agentName: "assistant",
            command: "ls", cwd: "/Users/test", expiresAtMilliseconds: 1_900_000_000_000
        )
        try await publish(shell: DesktopShellStatus(enabled: true, pending: request), through: helper, to: desktop)
        let controller = StatusMenuController(runner: idleRunner(), desktop: desktop)
        var selected: AppSection?
        controller.showWindow = { selected = $0 }
        let menu = NSMenu()

        controller.menuNeedsUpdate(menu)

        let status = try XCTUnwrap(menu.items.first { $0.title == "Shell command waiting for approval" })
        XCTAssertFalse(status.isEnabled)
        let review = try XCTUnwrap(menu.items.first { $0.title == "Review Command…" })
        XCTAssertTrue(review.isEnabled)
        XCTAssertFalse(menu.items.contains { $0.title.contains("Approve") || $0.title == "Revoke Shell Access" })
        XCTAssertTrue(NSApplication.shared.sendAction(try XCTUnwrap(review.action), to: review.target, from: review))
        XCTAssertEqual(selected, .computerAccess)
        XCTAssertTrue(actions.isEmpty, "Reviewing from the menu never answers the request")
    }

    func testShellAutoApprovalOffersRevokeFromTheMenu() async throws {
        var actions: [String] = []
        let helper = DesktopBridgeProcess()
        let desktop = DesktopControlStore(helper: helper, request: { action, _, _ in
            actions.append(action)
            return [:]
        })
        try await publish(shell: DesktopShellStatus(enabled: true, autoApproveUntilRevoked: true), through: helper, to: desktop)
        let controller = StatusMenuController(runner: idleRunner(), desktop: desktop)
        let menu = NSMenu()

        controller.menuNeedsUpdate(menu)

        XCTAssertNotNil(menu.items.first { $0.title == "Shell auto-approval until you stop it" })
        XCTAssertFalse(menu.items.contains { $0.title == "Review Command…" })
        let revoke = try XCTUnwrap(menu.items.first { $0.title == "Revoke Shell Access" })
        XCTAssertTrue(revoke.isEnabled)
        XCTAssertTrue(NSApplication.shared.sendAction(try XCTUnwrap(revoke.action), to: revoke.target, from: revoke))
        for _ in 0 ..< 10 where actions.isEmpty { await Task.yield() }
        XCTAssertEqual(actions, ["revoke_shell"])
    }

    private func idleRunner() -> MindRoomCommandRunner {
        MindRoomCommandRunner(processRunner: { _ in CommandResult(exitCode: 0, output: "") })
    }

    private func publish(
        shell: DesktopShellStatus, through helper: DesktopBridgeProcess, to desktop: DesktopControlStore
    ) async throws {
        let base = DesktopStatus.stopped
        let status = DesktopStatus(
            config: DesktopConfigStatus(
                state: "ready", revision: 1, enabled: true, controllerUserID: "@controller:example.org",
                controllerDeviceID: "DEVICE", allowedRequesterIDs: ["@person:example.org"],
                allowedAgentNames: ["assistant"], allowedAppIDs: [], shellEnabled: true
            ),
            pairing: base.pairing, helper: DesktopHelperStatus(state: "running", version: "test"),
            bridge: DesktopRuntimeStatus(state: "observe_only", activeAction: nil, lastError: nil),
            authority: base.authority, permissions: base.permissions, browser: base.browser,
            apps: [], capabilities: [], shell: shell
        )
        let received = expectation(description: "shell status received")
        let subscription = desktop.$status.filter { $0 == status }.prefix(1).sink { _ in received.fulfill() }
        let json = String(decoding: try JSONEncoder().encode(status), as: UTF8.self)
        _ = helper.decode(Data("{\"v\":1,\"type\":\"status\",\"sequence\":1,\"status\":\(json)}".utf8))
        await fulfillment(of: [received], timeout: 2)
        withExtendedLifetime(subscription) {}
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
