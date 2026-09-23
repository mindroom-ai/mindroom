import XCTest
@testable import MindRoom

@MainActor
final class DesktopControlStoreTests: XCTestCase {
    func testRefreshRetainsManuallyAddedAppMetadataAfterDeselecting() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let bundle = root.appendingPathComponent("Custom Editor.app")
        let contents = bundle.appendingPathComponent("Contents")
        try FileManager.default.createDirectory(at: contents, withIntermediateDirectories: true)
        let info = ["CFBundleIdentifier": "com.example.CustomEditor", "CFBundleName": "Custom Editor", "CFBundlePackageType": "APPL"]
        try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
            .write(to: contents.appendingPathComponent("Info.plist"))
        let store = DesktopControlStore()
        XCTAssertEqual(store.addApplication(at: bundle), "com.example.CustomEditor")
        store.selectedAppIDs = []

        store.refreshApplications()

        XCTAssertEqual(store.applications.first { $0.id == "com.example.CustomEditor" }?.name, "Custom Editor")
        XCTAssertTrue(store.selectedAppIDs.isEmpty)
    }

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
