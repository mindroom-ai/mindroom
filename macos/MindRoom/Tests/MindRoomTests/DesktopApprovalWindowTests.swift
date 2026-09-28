import AppKit
import XCTest
@testable import MindRoom

@MainActor
final class DesktopApprovalWindowTests: XCTestCase {
    func testNewRequestAppearsWithoutTakingFocusAndDismissalDoesNotApprove() async throws {
        _ = NSApplication.shared
        let helper = DesktopBridgeProcess()
        var decisions = 0
        let store = DesktopControlStore(helper: helper, request: { _, _, _ in
            decisions += 1
            return [:]
        })
        let controller = DesktopApprovalWindowController(store: store)
        controller.start()
        defer { controller.stop() }
        let first = request("first")
        try publish(first, through: helper)
        await settle()
        let window = try XCTUnwrap(controller.window)
        XCTAssertTrue(window.isVisible)
        XCTAssertFalse(window.isKeyWindow, "A remote request must not steal keyboard focus")
        XCTAssertTrue((window as? NSPanel)?.worksWhenModal == true, "Approval must work while a folder picker is open")
        XCTAssertGreaterThanOrEqual(window.level.rawValue, NSWindow.Level.modalPanel.rawValue)
        XCTAssertEqual(window.title, "Approve Shell Command")

        window.close()
        try publish(first, through: helper)
        await settle()
        XCTAssertFalse(window.isVisible, "Polling must not reopen a dismissed request")
        XCTAssertEqual(store.status.shell.pending?.requestID, "first")
        XCTAssertEqual(decisions, 0, "Closing or displaying a request never grants or rejects it")

        controller.showPending()
        XCTAssertTrue(window.isVisible, "An explicit review reopens the same request")
        window.close()
        try publish(request("second"), through: helper)
        await settle()
        XCTAssertTrue(window.isVisible, "A new request gets its own alert")

        try publish(nil, through: helper)
        await settle()
        XCTAssertFalse(window.isVisible, "Decisions made elsewhere dismiss the popup")
    }

    func testStoppingOrLosingTheHelperDismissesPendingApproval() async throws {
        _ = NSApplication.shared
        let helper = DesktopBridgeProcess()
        let store = DesktopControlStore(helper: helper)
        let controller = DesktopApprovalWindowController(store: store)
        controller.start()
        defer { controller.stop() }
        for state in ["stopping", "faulted", "stopped"] {
            try publish(request(state), through: helper)
            await settle()
            XCTAssertTrue(controller.window?.isVisible == true)
            try publish(request(state), bridge: state, through: helper)
            await settle()
            XCTAssertFalse(controller.window?.isVisible == true)
        }
    }

    func testExpiredRequestDoesNotOpenAndVisibleRequestClosesAtExpiry() async throws {
        _ = NSApplication.shared
        let helper = DesktopBridgeProcess()
        let store = DesktopControlStore(helper: helper)
        let controller = DesktopApprovalWindowController(store: store)
        controller.start()
        defer { controller.stop() }
        try publish(request("expired", expiresIn: -1), through: helper)
        await settle()
        XCTAssertFalse(controller.window?.isVisible == true)
        // The first SwiftUI window can take seconds to render on a cold CI runner.
        try publish(request("brief"), through: helper)
        await settle()
        XCTAssertTrue(controller.window?.isVisible == true)
        try publish(request("brief", expiresIn: 0.3), through: helper)
        // Even a stalled helper cannot leave an actionable expired popup on screen.
        let deadline = Date().addingTimeInterval(5)
        while controller.window?.isVisible == true && Date() < deadline { await settle() }
        XCTAssertFalse(controller.window?.isVisible == true)
    }

    private func request(_ id: String, expiresIn: TimeInterval = 60) -> DesktopShellRequest {
        DesktopShellRequest(
            requestID: id, requesterID: "@person:example.org", agentName: "assistant",
            command: "printf hello", cwd: "/Users/test",
            expiresAtMilliseconds: Date().addingTimeInterval(expiresIn).timeIntervalSince1970 * 1000
        )
    }

    private func publish(_ request: DesktopShellRequest?, bridge: String = "observe_only", through helper: DesktopBridgeProcess) throws {
        let base = DesktopStatus.stopped
        let status = DesktopStatus(
            config: base.config, pairing: base.pairing, helper: base.helper,
            bridge: DesktopRuntimeStatus(state: bridge, activeAction: nil, lastError: nil),
            authority: base.authority, permissions: base.permissions, browser: base.browser,
            apps: base.apps, capabilities: base.capabilities,
            shell: DesktopShellStatus(enabled: true, pending: request)
        )
        let json = String(decoding: try JSONEncoder().encode(status), as: UTF8.self)
        _ = helper.decode(Data("{\"v\":1,\"type\":\"status\",\"sequence\":1,\"status\":\(json)}".utf8))
    }

    private func settle() async { try? await Task.sleep(for: .milliseconds(50)) }
}
