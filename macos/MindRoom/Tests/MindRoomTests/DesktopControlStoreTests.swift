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

        store.saveAndConnect()

        XCTAssertEqual(store.errorMessage, "Confirm the displayed controller, requester, and agent before saving.")
        XCTAssertNil(store.recovery)
    }

    func testCancelImportedSetupRestoresSavedIdentitiesButKeepsAppAndBrowserDrafts() async throws {
        let helper = DesktopBridgeProcess()
        let store = DesktopControlStore(helper: helper)
        try await publish(configuredStatus(
            revision: 1, apps: ["com.example.Editor"], homeserver: "https://saved.example.org", userID: "@saved:example.org"
        ), through: helper, to: store)
        store.homeserver = "https://draft.example.org"
        store.matrixUserID = "@draft:example.org"
        store.controllerUserID = "@draft-controller:example.org"
        store.controllerDeviceID = "DRAFT"
        store.controllerFingerprint = "draft-key"
        store.requesterIDs = "@draft:example.org"
        store.agentNames = "draft-agent"
        store.selectedAppIDs = ["com.example.Draft"]
        store.browserEnabled = true
        store.browserProfile = "/draft-profile"
        store.identityConfirmed = true

        store.cancelSetupImport()

        XCTAssertEqual(store.homeserver, "https://saved.example.org")
        XCTAssertEqual(store.matrixUserID, "@saved:example.org")
        XCTAssertEqual(store.controllerUserID, "@controller:example.org")
        XCTAssertEqual(store.controllerDeviceID, "DEVICE")
        XCTAssertEqual(store.controllerFingerprint, "key")
        XCTAssertEqual(store.requesterIDs, "@person:example.org")
        XCTAssertEqual(store.agentNames, "assistant")
        XCTAssertEqual(store.selectedAppIDs, ["com.example.Draft"])
        XCTAssertTrue(store.browserEnabled)
        XCTAssertEqual(store.browserProfile, "/draft-profile")
        XCTAssertFalse(store.identityConfirmed)
    }

    func testSaveAndConnectKeepsSavedConfigurationDisabledWhileAwaitingChatConfirmation() async throws {
        let helper = DesktopBridgeProcess()
        let pending = configuredStatus(
            revision: 2, apps: ["com.example.Editor"], homeserver: "https://example.org", userID: "@person:example.org", enabled: false
        )
        var requests: [(String, [String: Any])] = []
        let store = DesktopControlStore(helper: helper, request: { action, parameters, _ in
            requests.append((action, parameters))
            var result = try self.response(status: pending)
            if action == "pair" {
                result["verification"] = "ABCD-EFGH"
                result["confirmation_command"] = "!desktop confirm one-time ABCD-EFGH"
            }
            return result
        })
        try await publish(configuredStatus(
            revision: 1, apps: ["com.example.Editor"], homeserver: "https://example.org", userID: "@person:example.org"
        ), through: helper, to: store)
        store.identityConfirmed = true
        store.pairingCode = "one-time"

        store.saveAndConnect()
        await waitUntilIdle(store)

        XCTAssertEqual(requests.map(\.0), ["configure", "pair"])
        XCTAssertEqual((requests[0].1["config"] as? [String: Any])?["enabled"] as? Bool, false)
        XCTAssertEqual(requests[1].1["expected_revision"] as? Int, 2)
        XCTAssertFalse(store.confirmationCommand.isEmpty)
        try await publish(pending, through: helper, to: store)
        XCTAssertFalse(store.status.hasSavedConnection)
    }

    func testConfigureResponseCannotAdoptAnExternalReplacementRevisionForPairing() async throws {
        let helper = DesktopBridgeProcess()
        var actions: [String] = []
        let replacement = configuredStatus(
            revision: 3, apps: ["com.example.Editor"], homeserver: "https://example.org", userID: "@person:example.org", enabled: false
        )
        let store = DesktopControlStore(helper: helper, request: { action, _, _ in
            actions.append(action)
            return try self.response(status: replacement)
        })
        try await publish(configuredStatus(
            revision: 1, apps: ["com.example.Editor"], homeserver: "https://example.org", userID: "@person:example.org"
        ), through: helper, to: store)
        store.identityConfirmed = true
        store.pairingCode = "one-time"

        store.saveAndConnect()
        await waitUntilIdle(store)

        XCTAssertEqual(actions, ["configure"])
        XCTAssertNotNil(store.errorMessage)
        XCTAssertTrue(store.confirmationCommand.isEmpty)
    }

    func testChatConfirmationEnablesClaimedSnapshotWithoutSavingLaterDraftEdits() async throws {
        let helper = DesktopBridgeProcess()
        let pending = configuredStatus(
            revision: 2, apps: ["com.example.Editor"], homeserver: "https://example.org", userID: "@person:example.org", enabled: false
        )
        let finished = configuredStatus(
            revision: 3, apps: ["com.example.Editor"], homeserver: "https://example.org", userID: "@person:example.org"
        )
        var requests: [(String, [String: Any])] = []
        let store = DesktopControlStore(helper: helper, request: { action, parameters, _ in
            requests.append((action, parameters))
            var result = try self.response(status: action == "finish_setup" ? finished : pending)
            if action == "pair" {
                result["verification"] = "ABCD-EFGH"
                result["confirmation_command"] = "!desktop confirm one-time ABCD-EFGH"
            }
            return result
        })
        try await prepareClaim(store, helper: helper, pending: pending)
        store.controllerUserID = "@different:example.org"
        store.selectedAppIDs = ["com.example.Draft"]
        var completed = false

        store.finishChatConfirmation { completed = true }
        await waitUntilIdle(store)

        XCTAssertEqual(requests.map(\.0), ["configure", "pair", "finish_setup"])
        let finish = try XCTUnwrap(requests.last?.1)
        XCTAssertEqual(Set(finish.keys), ["expected_revision", "expected_session"])
        XCTAssertEqual(finish["expected_revision"] as? Int, 2)
        XCTAssertEqual(finish["expected_session"] as? [String: String], [
            "homeserver": "https://example.org", "user_id": "@person:example.org", "device_id": "LOCAL",
        ])
        XCTAssertTrue(completed)
        XCTAssertTrue(store.confirmationCommand.isEmpty)
        XCTAssertFalse(store.needsPairing)
    }

    func testChatConfirmationKeepsPendingCommandWhenHelperRejectsActivation() async throws {
        let helper = DesktopBridgeProcess()
        let pending = configuredStatus(
            revision: 2, apps: ["com.example.Editor"], homeserver: "https://example.org", userID: "@person:example.org", enabled: false
        )
        let store = DesktopControlStore(helper: helper, request: { action, _, _ in
            if action == "finish_setup" {
                throw DesktopBridgeProcessError.helper(DesktopBridgeErrorPayload(
                    code: "session_conflict", message: "Session changed", recovery: nil, retryable: false
                ))
            }
            var result = try self.response(status: pending)
            if action == "pair" {
                result["verification"] = "ABCD-EFGH"
                result["confirmation_command"] = "!desktop confirm one-time ABCD-EFGH"
            }
            return result
        })
        try await prepareClaim(store, helper: helper, pending: pending)
        var completed = false

        store.finishChatConfirmation { completed = true }
        await waitUntilIdle(store)

        XCTAssertFalse(completed)
        XCTAssertEqual(store.errorMessage, "Session changed")
        XCTAssertFalse(store.confirmationCommand.isEmpty)
        XCTAssertTrue(store.needsPairing)
    }

    func testChatConfirmationRejectsChangedConfigurationOrLoginBeforeActivation() async throws {
        for changedSession in [false, true] {
            let helper = DesktopBridgeProcess()
            let pending = configuredStatus(
                revision: 2, apps: ["com.example.Editor"], homeserver: "https://example.org", userID: "@person:example.org", enabled: false
            )
            var actions: [String] = []
            let store = DesktopControlStore(helper: helper, request: { action, _, _ in
                actions.append(action)
                var result = try self.response(status: pending)
                if action == "pair" {
                    result["verification"] = "ABCD-EFGH"
                    result["confirmation_command"] = "!desktop confirm one-time ABCD-EFGH"
                }
                return result
            })
            try await prepareClaim(store, helper: helper, pending: pending)
            try await publish(configuredStatus(
                revision: changedSession ? 2 : 3, apps: ["com.example.Editor"], homeserver: "https://example.org",
                userID: "@person:example.org", enabled: false, deviceID: changedSession ? "REPLACEMENT" : "LOCAL"
            ), through: helper, to: store)

            store.finishChatConfirmation()
            await waitUntilIdle(store)

            XCTAssertEqual(actions, ["configure", "pair"])
            XCTAssertNotNil(store.errorMessage)
            XCTAssertFalse(store.confirmationCommand.isEmpty)
        }
    }

    func testBrowserSaveDoesNotPublishHiddenConnectionDrafts() async throws {
        let helper = DesktopBridgeProcess()
        let saved = configuredStatus(
            revision: 4, apps: ["com.example.Editor"], homeserver: "https://example.org", userID: "@person:example.org"
        )
        var requests: [(String, [String: Any])] = []
        let store = DesktopControlStore(helper: helper, request: { action, parameters, _ in
            requests.append((action, parameters))
            return try self.response(status: saved)
        })
        try await publish(saved, through: helper, to: store)
        store.controllerUserID = "@draft-controller:example.org"
        store.selectedAppIDs = ["com.example.Draft"]
        store.browserEnabled = true
        store.browserExecutable = "/Applications/Browser.app"
        store.browserProfile = "/browser-profile"

        store.saveBrowserConfiguration()
        await waitUntilIdle(store)

        XCTAssertEqual(requests.map(\.0), ["set_browser_config"])
        let parameters = try XCTUnwrap(requests.first?.1)
        XCTAssertEqual(Set(parameters.keys), ["expected_revision", "browser"])
        XCTAssertEqual(parameters["expected_revision"] as? Int, 4)
        let browser = try XCTUnwrap(parameters["browser"] as? [String: Any])
        XCTAssertEqual(browser["enabled"] as? Bool, true)
        XCTAssertEqual(browser["executable_path"] as? String, "/Applications/Browser.app")
        XCTAssertEqual(browser["user_data_dir"] as? String, "/browser-profile")
        XCTAssertTrue(store.status.config.enabled)
    }

    func testPendingSetupCannotSaveAppsOrBrowserSettings() async throws {
        for existingConnection in [false, true] {
            let helper = DesktopBridgeProcess()
            let saved = configuredStatus(
                revision: 4, apps: ["com.example.Editor"], homeserver: "https://example.org",
                userID: "@person:example.org", enabled: existingConnection
            )
            let descriptor: [String: Any] = [
                "v": 1, "kind": "mindroom_desktop_setup", "homeserver": "https://example.org", "user_id": "@person:example.org",
                "code": "one-time", "controller_user_id": "@controller:example.org", "controller_device_id": "DEVICE",
                "controller_ed25519": "key", "requester_id": "@person:example.org", "agent_name": "assistant", "cloudflare_access": false,
            ]
            var actions: [String] = []
            let store = DesktopControlStore(helper: helper, request: { action, _, _ in
                if action == "import_setup" { return descriptor }
                actions.append(action)
                return try self.response(status: saved)
            })
            try await publish(saved, through: helper, to: store)
            if existingConnection {
                store.setupDescriptor = String(decoding: try JSONSerialization.data(withJSONObject: descriptor), as: UTF8.self)
                store.importSetupDescriptor()
                await waitUntilIdle(store)
                XCTAssertTrue(store.needsPairing)
            }

            store.saveAllowedApplications()
            await waitUntilIdle(store)
            store.saveBrowserConfiguration()
            await waitUntilIdle(store)

            XCTAssertTrue(actions.isEmpty)
            XCTAssertNotNil(store.errorMessage)
        }
    }

    private func prepareClaim(_ store: DesktopControlStore, helper: DesktopBridgeProcess, pending: DesktopStatus) async throws {
        try await publish(configuredStatus(
            revision: 1, apps: ["com.example.Editor"], homeserver: "https://example.org", userID: "@person:example.org"
        ), through: helper, to: store)
        store.identityConfirmed = true
        store.pairingCode = "one-time"
        store.saveAndConnect()
        await waitUntilIdle(store)
        try await publish(pending, through: helper, to: store)
    }

    private func waitUntilIdle(_ store: DesktopControlStore) async {
        let finished = expectation(description: "store operation finished")
        let subscription = store.$isBusy.filter { !$0 }.prefix(1).sink { _ in finished.fulfill() }
        await fulfillment(of: [finished], timeout: 2)
        withExtendedLifetime(subscription) {}
    }

    private func response(status: DesktopStatus) throws -> [String: Any] {
        ["status": try JSONSerialization.jsonObject(with: JSONEncoder().encode(status))]
    }

    private func configuredStatus(
        revision: Int, apps: [String],
        controllerUserID: String = "@controller:example.org", controllerDeviceID: String = "DEVICE",
        controllerFingerprint: String = "key", requesterIDs: [String] = ["@person:example.org"],
        agentNames: [String] = ["assistant"], homeserver: String? = nil, userID: String? = nil,
        browser: DesktopBrowserStatus = DesktopStatus.stopped.browser, enabled: Bool = true, deviceID: String = "LOCAL"
    ) -> DesktopStatus {
        let base = DesktopStatus.stopped
        return DesktopStatus(
            config: DesktopConfigStatus(
                state: "ready", revision: revision, enabled: enabled,
                controllerUserID: controllerUserID, controllerDeviceID: controllerDeviceID,
                allowedRequesterIDs: requesterIDs, allowedAgentNames: agentNames, allowedAppIDs: apps
            ),
            pairing: DesktopPairingStatus(
                state: "unpaired", sessionState: userID == nil ? .missing : .ready,
                homeserver: homeserver, userID: userID, deviceID: userID == nil ? nil : deviceID,
                controllerFingerprint: controllerFingerprint
            ),
            helper: base.helper, bridge: base.bridge,
            authority: base.authority, permissions: base.permissions,
            browser: browser, apps: base.apps, capabilities: base.capabilities
        )
    }

    private func publish(_ status: DesktopStatus, through helper: DesktopBridgeProcess, to store: DesktopControlStore) async throws {
        let received = expectation(description: "configuration revision \(status.config.revision) received")
        let subscription = store.$status.filter { $0 == status }.prefix(1)
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
