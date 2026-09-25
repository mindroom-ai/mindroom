import XCTest
@testable import MindRoom

final class DesktopControlPresentationTests: XCTestCase {
    func testSavedConfigurationWithoutLoginStillRequiresConnectionSetup() {
        let value = status(bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"], session: .missing)
        XCTAssertEqual(value.startBlocker(isBusy: false, hasAppSelectionChanges: false), .setup)
    }

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

    func testSetupProgressFollowsConnectionAppsPermissionsThenStart() {
        let disconnected = status(bridge: "stopped", helper: "ready")
        XCTAssertEqual(disconnected.nextSetupSection(hasAppSelectionChanges: false), .setup)
        let noApps = status(bridge: "stopped", helper: "ready", config: "ready", permissionsGranted: false)
        XCTAssertEqual(noApps.nextSetupSection(hasAppSelectionChanges: false), .applications)
        let needsPermissions = status(bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"], permissionsGranted: false)
        XCTAssertEqual(needsPermissions.nextSetupSection(hasAppSelectionChanges: false), .permissions)
        XCTAssertEqual(needsPermissions.startBlocker(isBusy: false, hasAppSelectionChanges: false), .permissions)
        let ready = status(bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"])
        XCTAssertEqual(ready.nextSetupSection(hasAppSelectionChanges: false), .session)
        XCTAssertEqual(ready.nextSetupSection(hasAppSelectionChanges: true), .applications)
        XCTAssertEqual(ready.nextSetupSection(hasAppSelectionChanges: false, needsPairing: true), .setup)
    }

    func testSavedConnectionDoesNotRequireReconnectingAfterAppRestart() {
        let saved = status(bridge: "stopped", helper: "ready", config: "ready")
        XCTAssertTrue(saved.hasSavedConnection)
        XCTAssertEqual(saved.nextSetupSection(hasAppSelectionChanges: false), .applications)
        let loggedOut = status(bridge: "stopped", helper: "ready", config: "ready", session: .missing)
        XCTAssertFalse(loggedOut.hasSavedConnection)
        XCTAssertEqual(loggedOut.nextSetupSection(hasAppSelectionChanges: false), .setup)
    }

    func testPendingPairingCannotStartUsingPreviouslySavedConnection() {
        let saved = status(bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"])
        XCTAssertEqual(saved.startBlocker(isBusy: false, hasAppSelectionChanges: false, needsPairing: true), .setup)
    }

    func testConnectionTitleDoesNotClaimAccessDuringTransitionsOrFailure() {
        XCTAssertEqual(status(bridge: "stopped", helper: "starting").connectionTitle, "Connecting…")
        XCTAssertEqual(status(bridge: "stopping", helper: "stopping").connectionTitle, "Stopping…")
        XCTAssertEqual(status(bridge: "faulted", helper: "running").connectionTitle, "Connection needs attention")
        XCTAssertEqual(status(bridge: "observe_only", helper: "running").connectionTitle, "Connected")
    }

    func testAppSelectionCannotPublishOverIncompletePairing() {
        let incomplete = status(bridge: "stopped", helper: "ready", config: "ready", enabled: false)
        XCTAssertEqual(incomplete.appSelectionAction, .setup)
        XCTAssertFalse(incomplete.hasSavedConnection)
        XCTAssertEqual(incomplete.nextSetupSection(hasAppSelectionChanges: true), .setup)
    }

    func testStepChecksRequireSavedChoicesAndCompletedConnection() {
        let saved = status(bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"])
        XCTAssertEqual(saved.setupProgress(for: .setup, needsPairing: false, hasAppSelectionChanges: false), .complete("Saved"))
        XCTAssertEqual(saved.setupProgress(for: .setup, needsPairing: true, hasAppSelectionChanges: false), .needsAction("Finish setup"))
        XCTAssertEqual(saved.setupProgress(for: .applications, needsPairing: false, hasAppSelectionChanges: false), .complete("1 app"))
        XCTAssertEqual(saved.setupProgress(for: .applications, needsPairing: false, hasAppSelectionChanges: true), .needsAction("Unsaved changes"))
        let empty = status(bridge: "stopped", helper: "ready", config: "ready")
        XCTAssertEqual(empty.setupProgress(for: .applications, needsPairing: false, hasAppSelectionChanges: false), .needsAction("No apps selected"))
    }

    func testPermissionAndStartChecksReflectActualRuntimeState() {
        let missing = status(bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"], permissionsGranted: false)
        XCTAssertEqual(missing.setupProgress(for: .permissions, needsPairing: false, hasAppSelectionChanges: false), .needsAction("Not allowed"))
        XCTAssertEqual(missing.setupProgress(for: .session, needsPairing: false, hasAppSelectionChanges: false), .idle("Off"))
        let ready = status(bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"])
        XCTAssertEqual(ready.setupProgress(for: .permissions, needsPairing: false, hasAppSelectionChanges: false), .complete("Allowed"))
        XCTAssertEqual(ready.setupProgress(for: .session, needsPairing: false, hasAppSelectionChanges: false), .idle("Ready"))
        let active = status(bridge: "observe_only", helper: "running", config: "ready", apps: ["com.apple.TextEdit"])
        XCTAssertEqual(active.setupProgress(for: .session, needsPairing: false, hasAppSelectionChanges: false), .complete("Running"))
    }

    private func status(
        bridge: String, helper: String, config: String = "missing", apps: [String] = [],
        session: DesktopSessionState = .ready, permissionsGranted: Bool = true, enabled: Bool = true
    ) -> DesktopStatus {
        let base = DesktopStatus.stopped
        return DesktopStatus(
            config: DesktopConfigStatus(
                state: config, revision: 1, enabled: enabled,
                controllerUserID: nil, controllerDeviceID: nil,
                allowedRequesterIDs: nil, allowedAgentNames: nil, allowedAppIDs: apps
            ),
            pairing: DesktopPairingStatus(
                state: "unpaired", sessionState: session, homeserver: "https://example.org",
                userID: "@person:example.org", deviceID: "DEVICE", controllerFingerprint: "key"
            ),
            helper: DesktopHelperStatus(state: helper, version: "test"),
            bridge: DesktopRuntimeStatus(state: bridge, activeAction: nil, lastError: nil),
            authority: base.authority, permissions: DesktopPermissionsStatus(
                accessibility: DesktopPermissionStatus(state: permissionsGranted ? "granted" : "missing", canRequest: true, recovery: nil),
                screenRecording: DesktopPermissionStatus(state: permissionsGranted ? "granted" : "missing", canRequest: true, recovery: nil)
            ),
            browser: base.browser, apps: base.apps, capabilities: base.capabilities
        )
    }
}
