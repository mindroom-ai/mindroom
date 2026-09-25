import Combine
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

    func testStatusRefreshPreservesDeselectedAppsAcrossExternalConfigurationChanges() {
        let store = DesktopControlStore()
        store.hydrateConfiguration(from: configuredStatus(revision: 1, apps: ["com.example.Editor"]))
        XCTAssertEqual(store.selectedAppIDs, ["com.example.Editor"])

        store.selectedAppIDs = []
        store.hydrateConfiguration(from: configuredStatus(revision: 1, apps: ["com.example.Editor"]))
        XCTAssertTrue(store.selectedAppIDs.isEmpty)

        store.hydrateConfiguration(from: configuredStatus(revision: 2, apps: ["com.example.Browser"]))
        XCTAssertTrue(store.selectedAppIDs.isEmpty)
    }

    func testExternalConfigurationRefreshesUneditedSetupFields() {
        let store = DesktopControlStore()
        store.hydrateConfiguration(from: configuredStatus(revision: 1, apps: ["com.example.Editor"]))
        store.identityConfirmed = true

        store.hydrateConfiguration(from: configuredStatus(
            revision: 2, apps: ["com.example.Browser"],
            controllerUserID: "@new-controller:example.org", controllerDeviceID: "NEW-DEVICE",
            controllerFingerprint: "new-key", requesterIDs: ["@new-person:example.org"], agentNames: ["researcher"],
            browser: configuredBrowser(executable: "/Applications/New.app", profile: "/new-profile")
        ))

        XCTAssertEqual(store.controllerUserID, "@new-controller:example.org")
        XCTAssertEqual(store.controllerDeviceID, "NEW-DEVICE")
        XCTAssertEqual(store.controllerFingerprint, "new-key")
        XCTAssertEqual(store.requesterIDs, "@new-person:example.org")
        XCTAssertEqual(store.agentNames, "researcher")
        XCTAssertEqual(store.selectedAppIDs, ["com.example.Browser"])
        XCTAssertTrue(store.browserEnabled)
        XCTAssertEqual(store.browserExecutable, "/Applications/New.app")
        XCTAssertEqual(store.browserProfile, "/new-profile")
        XCTAssertFalse(store.identityConfirmed)
    }

    func testExternalConfigurationPreservesSetupDraftsIncludingClearedFields() {
        let store = DesktopControlStore()
        store.hydrateConfiguration(from: configuredStatus(
            revision: 1, apps: ["com.example.Editor"],
            browser: configuredBrowser(executable: "/Applications/Old.app", profile: "/old-profile")
        ))
        store.controllerUserID = ""
        store.controllerDeviceID = "DRAFT-DEVICE"
        store.controllerFingerprint = ""
        store.requesterIDs = ""
        store.agentNames = "draft-agent"
        store.selectedAppIDs = []
        store.browserEnabled = false
        store.browserExecutable = ""
        store.browserProfile = "/draft-profile"
        store.identityConfirmed = true

        store.hydrateConfiguration(from: configuredStatus(
            revision: 2, apps: ["com.example.Browser"],
            controllerUserID: "@new-controller:example.org", controllerDeviceID: "NEW-DEVICE",
            controllerFingerprint: "new-key", requesterIDs: ["@new-person:example.org"], agentNames: ["researcher"],
            browser: configuredBrowser(executable: "/Applications/New.app", profile: "/new-profile")
        ))

        XCTAssertEqual(store.controllerUserID, "")
        XCTAssertEqual(store.controllerDeviceID, "DRAFT-DEVICE")
        XCTAssertEqual(store.controllerFingerprint, "")
        XCTAssertEqual(store.requesterIDs, "")
        XCTAssertEqual(store.agentNames, "draft-agent")
        XCTAssertTrue(store.selectedAppIDs.isEmpty)
        XCTAssertFalse(store.browserEnabled)
        XCTAssertEqual(store.browserExecutable, "")
        XCTAssertEqual(store.browserProfile, "/draft-profile")
        XCTAssertFalse(store.identityConfirmed)
    }

    func testFirstSavedConfigurationPreservesPreparedAppAndBrowserDrafts() {
        let store = DesktopControlStore()
        store.selectedAppIDs = ["com.example.Draft"]
        store.browserEnabled = true
        store.browserExecutable = "/Applications/Draft.app"
        store.browserProfile = "/draft-profile"

        store.hydrateConfiguration(from: configuredStatus(revision: 1, apps: ["com.example.Editor"]))

        XCTAssertEqual(store.selectedAppIDs, ["com.example.Draft"])
        XCTAssertTrue(store.browserEnabled)
        XCTAssertEqual(store.browserExecutable, "/Applications/Draft.app")
        XCTAssertEqual(store.browserProfile, "/draft-profile")
        XCTAssertEqual(store.controllerUserID, "@controller:example.org")
    }

    func testFirstSavedConfigurationPreservesDraftFieldsReturnedToDefaults() {
        let store = DesktopControlStore()
        store.controllerUserID = "@draft:example.org"
        store.controllerUserID = ""
        store.controllerDeviceID = "DRAFT"
        store.controllerDeviceID = ""
        store.controllerFingerprint = "draft-key"
        store.controllerFingerprint = ""
        store.requesterIDs = "@draft:example.org"
        store.requesterIDs = ""
        store.agentNames = "draft-agent"
        store.agentNames = ""
        store.selectedAppIDs = ["com.example.Draft"]
        store.selectedAppIDs = []
        store.browserEnabled = true
        store.browserEnabled = false
        store.browserExecutable = "/Applications/Draft.app"
        store.browserExecutable = ""
        store.browserProfile = "/draft-profile"
        store.browserProfile = ""

        store.hydrateConfiguration(from: configuredStatus(
            revision: 1, apps: ["com.example.Editor"],
            browser: configuredBrowser(executable: "/Applications/Saved.app", profile: "/saved-profile")
        ))

        XCTAssertEqual(store.controllerUserID, "")
        XCTAssertEqual(store.controllerDeviceID, "")
        XCTAssertEqual(store.controllerFingerprint, "")
        XCTAssertEqual(store.requesterIDs, "")
        XCTAssertEqual(store.agentNames, "")
        XCTAssertTrue(store.selectedAppIDs.isEmpty)
        XCTAssertFalse(store.browserEnabled)
        XCTAssertEqual(store.browserExecutable, "")
        XCTAssertEqual(store.browserProfile, "")
    }

    func testFirstSavedSessionPreservesLoginIdentityReturnedToDefaults() {
        for editHomeserver in [false, true] {
            let store = DesktopControlStore()
            if editHomeserver {
                store.homeserver = "https://draft.example.org"
                store.homeserver = "https://mindroom.chat"
            } else {
                store.matrixUserID = "@draft:example.org"
                store.matrixUserID = ""
            }

            store.hydrateConfiguration(from: configuredStatus(
                revision: 1, apps: [], homeserver: "https://saved.example.org", userID: "@saved:saved.example.org"
            ))

            XCTAssertEqual(store.homeserver, "https://mindroom.chat")
            XCTAssertEqual(store.matrixUserID, "")
        }
    }

    func testExternalSessionReplacementRefreshesUneditedLoginFields() {
        let store = DesktopControlStore()
        store.hydrateConfiguration(from: configuredStatus(
            revision: 1, apps: [], homeserver: "https://old.example.org", userID: "@old:old.example.org"
        ))
        store.identityConfirmed = true

        store.hydrateConfiguration(from: configuredStatus(
            revision: 1, apps: [], homeserver: "https://new.example.org", userID: "@new:new.example.org"
        ))

        XCTAssertEqual(store.homeserver, "https://new.example.org")
        XCTAssertEqual(store.matrixUserID, "@new:new.example.org")
        XCTAssertFalse(store.identityConfirmed)
    }

    func testExternalSessionReplacementPreservesPreparedLoginFields() {
        let store = DesktopControlStore()
        store.hydrateConfiguration(from: configuredStatus(
            revision: 1, apps: [], homeserver: "https://old.example.org", userID: "@old:old.example.org"
        ))
        store.homeserver = "https://draft.example.org"
        store.matrixUserID = ""

        store.hydrateConfiguration(from: configuredStatus(
            revision: 1, apps: [], homeserver: "https://new.example.org", userID: "@new:new.example.org"
        ))

        XCTAssertEqual(store.homeserver, "https://draft.example.org")
        XCTAssertEqual(store.matrixUserID, "")
    }

    func testSavedSessionPreservesBlankUserIDForPreparedSSOHomeserver() {
        let store = DesktopControlStore()
        store.homeserver = "https://draft.example.org"

        store.hydrateConfiguration(from: configuredStatus(
            revision: 1, apps: [], homeserver: "https://saved.example.org", userID: "@saved:saved.example.org"
        ))

        XCTAssertEqual(store.homeserver, "https://draft.example.org")
        XCTAssertEqual(store.matrixUserID, "")
    }

    func testExternalSessionPreservesHomeserverWhenUserIDWasClearedForSSO() {
        let store = DesktopControlStore()
        store.hydrateConfiguration(from: configuredStatus(
            revision: 1, apps: [], homeserver: "https://old.example.org", userID: "@old:old.example.org"
        ))
        store.matrixUserID = ""

        store.hydrateConfiguration(from: configuredStatus(
            revision: 1, apps: [], homeserver: "https://new.example.org", userID: "@new:new.example.org"
        ))

        XCTAssertEqual(store.homeserver, "https://old.example.org")
        XCTAssertEqual(store.matrixUserID, "")
    }

    func testDiscardAppDraftUsesLatestExternalSelectionAndResumesRefresh() async throws {
        let helper = DesktopBridgeProcess()
        let store = DesktopControlStore(helper: helper)
        try await publish(configuredStatus(revision: 1, apps: ["com.example.Editor"]), through: helper, to: store)
        store.selectedAppIDs = ["com.example.Draft"]
        try await publish(configuredStatus(revision: 2, apps: ["com.example.Browser"]), through: helper, to: store)
        XCTAssertEqual(store.selectedAppIDs, ["com.example.Draft"])
        XCTAssertTrue(store.hasAppSelectionChanges)

        store.discardAppSelectionChanges()

        XCTAssertEqual(store.selectedAppIDs, ["com.example.Browser"])
        XCTAssertFalse(store.hasAppSelectionChanges)
        try await publish(configuredStatus(revision: 3, apps: ["com.example.Terminal"]), through: helper, to: store)
        XCTAssertEqual(store.selectedAppIDs, ["com.example.Terminal"])
        XCTAssertFalse(store.hasAppSelectionChanges)
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

    private func configuredStatus(
        revision: Int, apps: [String],
        controllerUserID: String = "@controller:example.org", controllerDeviceID: String = "DEVICE",
        controllerFingerprint: String = "key", requesterIDs: [String] = ["@person:example.org"],
        agentNames: [String] = ["assistant"], homeserver: String? = nil, userID: String? = nil,
        browser: DesktopBrowserStatus = DesktopStatus.stopped.browser
    ) -> DesktopStatus {
        let base = DesktopStatus.stopped
        return DesktopStatus(
            config: DesktopConfigStatus(
                state: "ready", revision: revision, enabled: true,
                controllerUserID: controllerUserID, controllerDeviceID: controllerDeviceID,
                allowedRequesterIDs: requesterIDs, allowedAgentNames: agentNames, allowedAppIDs: apps
            ),
            pairing: DesktopPairingStatus(
                state: "unpaired", sessionState: userID == nil ? .missing : .ready,
                homeserver: homeserver, userID: userID, deviceID: userID == nil ? nil : "LOCAL",
                controllerFingerprint: controllerFingerprint
            ),
            helper: base.helper, bridge: base.bridge,
            authority: base.authority, permissions: base.permissions,
            browser: browser, apps: base.apps, capabilities: base.capabilities
        )
    }

    private func publish(_ status: DesktopStatus, through helper: DesktopBridgeProcess, to store: DesktopControlStore) async throws {
        let received = expectation(description: "configuration revision \(status.config.revision) received")
        let subscription = store.$status.filter { $0.config.revision == status.config.revision }.prefix(1)
            .sink { _ in received.fulfill() }
        let json = String(decoding: try JSONEncoder().encode(status), as: UTF8.self)
        _ = helper.decode(Data("{\"v\":1,\"type\":\"status\",\"sequence\":\(status.config.revision),\"status\":\(json)}".utf8))
        await fulfillment(of: [received], timeout: 2)
        withExtendedLifetime(subscription) {}
    }

    private func configuredBrowser(executable: String, profile: String) -> DesktopBrowserStatus {
        DesktopBrowserStatus(
            configured: true, executablePath: executable, userDataDirectory: profile,
            runtime: "available", extensionState: "disconnected", reconnectTokenConfigured: false, lastError: nil
        )
    }
}
