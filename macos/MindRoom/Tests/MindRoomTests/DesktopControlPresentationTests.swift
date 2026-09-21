import XCTest
@testable import MindRoom

final class DesktopControlPresentationTests: XCTestCase {
    func testStartupCanBeStoppedBeforeBridgeBecomesOnline() {
        XCTAssertTrue(status(bridge: "stopped", helper: "starting").canStopBridge)
    }

    func testIdleHelperDoesNotOfferBridgeStop() {
        XCTAssertFalse(DesktopStatus.stopped.canStopBridge)
        XCTAssertFalse(status(bridge: "stopped", helper: "ready").canStopBridge)
    }

    func testActiveAndFaultedBridgeCanBeStopped() {
        for state in ["observe_only", "control", "faulted"] {
            XCTAssertTrue(status(bridge: state, helper: "ready").canStopBridge)
        }
    }

    private func status(bridge: String, helper: String) -> DesktopStatus {
        let base = DesktopStatus.stopped
        return DesktopStatus(
            config: base.config, pairing: base.pairing,
            helper: DesktopHelperStatus(state: helper, version: "test"),
            bridge: DesktopRuntimeStatus(state: bridge, activeAction: nil, lastError: nil),
            authority: base.authority, permissions: base.permissions,
            browser: base.browser, apps: base.apps, capabilities: base.capabilities
        )
    }
}
