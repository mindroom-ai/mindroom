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

    func testMissingSetupExplainsStartAndOffersSetupInsteadOfSavingApps() {
        let value = status(bridge: "stopped", helper: "ready")

        XCTAssertEqual(value.startBlocker(isBusy: false, hasAppSelectionChanges: true), .setup)
        XCTAssertEqual(value.appSelectionAction, .setup)
        XCTAssertFalse(value.canStopBridge)
    }

    func testInvalidSetupStillOffersRepairInsteadOfSavingApps() {
        let value = status(bridge: "stopped", helper: "ready", config: "invalid")

        XCTAssertEqual(value.startBlocker(isBusy: false, hasAppSelectionChanges: false), .setup)
        XCTAssertEqual(value.appSelectionAction, .setup)
    }

    func testUnsavedSelectionExplainsStartEvenWhenSavedAppsAreEmpty() {
        let value = status(bridge: "stopped", helper: "ready", config: "ready")

        XCTAssertEqual(value.startBlocker(isBusy: false, hasAppSelectionChanges: true), .unsavedApps)
        XCTAssertEqual(value.appSelectionAction, .save)
    }

    func testSavedEmptySelectionMustChooseAppsBeforeStarting() {
        let value = status(bridge: "stopped", helper: "ready", config: "ready")

        XCTAssertEqual(value.startBlocker(isBusy: false, hasAppSelectionChanges: false), .noApps)
    }

    func testSavedAppsCanStartOnceOperationFinishes() {
        let value = status(bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"])

        XCTAssertEqual(value.startBlocker(isBusy: true, hasAppSelectionChanges: false), .busy)
        XCTAssertNil(value.startBlocker(isBusy: false, hasAppSelectionChanges: false))
    }

    func testStartupExplainsWaitWhileKeepingStopAvailable() {
        let value = status(bridge: "stopped", helper: "starting", config: "ready", apps: ["com.apple.TextEdit"])

        XCTAssertEqual(value.startBlocker(isBusy: true, hasAppSelectionChanges: false), .starting)
        XCTAssertTrue(value.canStopBridge)
        XCTAssertEqual(value.appSelectionAction, .stopAndSave)
    }

    func testRunningBridgeExplainsStartAndRequiresStopToSaveApps() {
        for state in ["observe_only", "control"] {
            let value = status(bridge: state, helper: "ready", config: "ready", apps: ["com.apple.TextEdit"])

            XCTAssertEqual(value.startBlocker(isBusy: false, hasAppSelectionChanges: true), .running)
            XCTAssertTrue(value.canStopBridge)
            XCTAssertEqual(value.appSelectionAction, .stopAndSave)
        }
        let faulted = status(bridge: "faulted", helper: "ready", config: "ready")
        XCTAssertEqual(faulted.startBlocker(isBusy: false, hasAppSelectionChanges: false), .faulted)
    }

    private func status(bridge: String, helper: String, config: String = "missing", apps: [String] = []) -> DesktopStatus {
        let base = DesktopStatus.stopped
        return DesktopStatus(
            config: DesktopConfigStatus(
                state: config, revision: 1, enabled: true,
                controllerUserID: nil, controllerDeviceID: nil,
                allowedRequesterIDs: nil, allowedAgentNames: nil, allowedAppIDs: apps
            ),
            pairing: base.pairing,
            helper: DesktopHelperStatus(state: helper, version: "test"),
            bridge: DesktopRuntimeStatus(state: bridge, activeAction: nil, lastError: nil),
            authority: base.authority, permissions: base.permissions,
            browser: base.browser, apps: base.apps, capabilities: base.capabilities
        )
    }
}
