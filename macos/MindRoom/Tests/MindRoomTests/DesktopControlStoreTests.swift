import XCTest
@testable import MindRoom

@MainActor
final class DesktopControlStoreTests: XCTestCase {
    func testStatusRefreshPreservesDeselectedAppsUntilConfigurationChanges() {
        let store = DesktopControlStore()
        store.hydrateConfiguration(from: configuredStatus(revision: 1, apps: ["com.example.Editor"]))
        XCTAssertEqual(store.selectedAppIDs, ["com.example.Editor"])

        store.selectedAppIDs = []
        store.hydrateConfiguration(from: configuredStatus(revision: 1, apps: ["com.example.Editor"]))
        XCTAssertTrue(store.selectedAppIDs.isEmpty)

        store.hydrateConfiguration(from: configuredStatus(revision: 2, apps: ["com.example.Browser"]))
        XCTAssertEqual(store.selectedAppIDs, ["com.example.Browser"])
    }

    func testUnconfirmedSaveClearsRecoveryFromPreviousImportError() {
        let store = DesktopControlStore()
        store.setupDescriptor = "not valid JSON"
        store.importSetupDescriptor()
        XCTAssertNotNil(store.recovery)

        store.saveConfiguration()

        XCTAssertEqual(store.errorMessage, "Confirm the displayed controller, requester, and agent before saving.")
        XCTAssertNil(store.recovery)
    }

    private func configuredStatus(revision: Int, apps: [String]) -> DesktopStatus {
        let base = DesktopStatus.stopped
        return DesktopStatus(
            config: DesktopConfigStatus(
                state: "ready", revision: revision, enabled: true,
                controllerUserID: "@controller:example.org", controllerDeviceID: "DEVICE",
                allowedRequesterIDs: ["@person:example.org"], allowedAgentNames: ["assistant"], allowedAppIDs: apps
            ),
            pairing: base.pairing, helper: base.helper, bridge: base.bridge,
            authority: base.authority, permissions: base.permissions,
            browser: base.browser, apps: base.apps, capabilities: base.capabilities
        )
    }
}
