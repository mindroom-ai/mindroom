import XCTest
@testable import MindRoom

final class DesktopControlPresentationTests: XCTestCase {
    func testSavedConfigurationWithoutLoginStillRequiresConnectionSetup() {
        let value = status(bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"], session: .missing)
        XCTAssertEqual(value.startBlocker(isBusy: false, hasAccessChanges: false), .setup)
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

        XCTAssertEqual(value.startBlocker(isBusy: false, hasAccessChanges: true), .setup)
        XCTAssertEqual(value.accessSaveAction, .setup)
        XCTAssertFalse(value.canStopBridge)
    }

    func testInvalidSetupStillOffersRepairInsteadOfSavingApps() {
        let value = status(bridge: "stopped", helper: "ready", config: "invalid")

        XCTAssertEqual(value.startBlocker(isBusy: false, hasAccessChanges: false), .setup)
        XCTAssertEqual(value.accessSaveAction, .setup)
    }

    func testUnsavedSelectionExplainsStartEvenWhenSavedAppsAreEmpty() {
        let value = status(bridge: "stopped", helper: "ready", config: "ready")

        XCTAssertEqual(value.startBlocker(isBusy: false, hasAccessChanges: true), .unsavedAccess)
        XCTAssertEqual(value.accessSaveAction, .save)
    }

    func testSavedEmptySelectionMustChooseAppsBeforeStarting() {
        let value = status(bridge: "stopped", helper: "ready", config: "ready")

        XCTAssertEqual(value.startBlocker(isBusy: false, hasAccessChanges: false), .noAccess)
    }

    func testSavedAppsCanStartOnceOperationFinishes() {
        let value = status(bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"])

        XCTAssertEqual(value.startBlocker(isBusy: true, hasAccessChanges: false), .busy)
        XCTAssertNil(value.startBlocker(isBusy: false, hasAccessChanges: false))
    }

    func testStartupExplainsWaitWhileKeepingStopAvailable() {
        let value = status(bridge: "stopped", helper: "starting", config: "ready", apps: ["com.apple.TextEdit"])

        XCTAssertEqual(value.startBlocker(isBusy: true, hasAccessChanges: false), .starting)
        XCTAssertTrue(value.canStopBridge)
        XCTAssertEqual(value.accessSaveAction, .stopAndSave)
    }

    func testRunningBridgeExplainsStartAndRequiresStopToSaveApps() {
        for state in ["observe_only", "control"] {
            let value = status(bridge: state, helper: "ready", config: "ready", apps: ["com.apple.TextEdit"])

            XCTAssertEqual(value.startBlocker(isBusy: false, hasAccessChanges: true), .running)
            XCTAssertTrue(value.canStopBridge)
            XCTAssertEqual(value.accessSaveAction, .stopAndSave)
        }
        let faulted = status(bridge: "faulted", helper: "ready", config: "ready")
        XCTAssertEqual(faulted.startBlocker(isBusy: false, hasAccessChanges: false), .faulted)
    }

    func testSetupProgressFollowsConnectionAppsPermissionsThenStart() {
        let disconnected = status(bridge: "stopped", helper: "ready")
        XCTAssertEqual(disconnected.nextSetupSection(hasAccessChanges: false), .setup)
        let noApps = status(bridge: "stopped", helper: "ready", config: "ready", permissionsGranted: false)
        XCTAssertEqual(noApps.nextSetupSection(hasAccessChanges: false), .access)
        let needsPermissions = status(bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"], permissionsGranted: false)
        XCTAssertEqual(needsPermissions.nextSetupSection(hasAccessChanges: false), .permissions)
        XCTAssertEqual(needsPermissions.startBlocker(isBusy: false, hasAccessChanges: false), .permissions)
        let ready = status(bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"])
        XCTAssertEqual(ready.nextSetupSection(hasAccessChanges: false), .session)
        XCTAssertEqual(ready.nextSetupSection(hasAccessChanges: true), .access)
        XCTAssertEqual(ready.nextSetupSection(hasAccessChanges: false, needsPairing: true), .setup)
    }

    func testSavedConnectionDoesNotRequireReconnectingAfterAppRestart() {
        let saved = status(bridge: "stopped", helper: "ready", config: "ready")
        XCTAssertTrue(saved.hasSavedConnection)
        XCTAssertEqual(saved.nextSetupSection(hasAccessChanges: false), .access)
        let loggedOut = status(bridge: "stopped", helper: "ready", config: "ready", session: .missing)
        XCTAssertFalse(loggedOut.hasSavedConnection)
        XCTAssertEqual(loggedOut.nextSetupSection(hasAccessChanges: false), .setup)
    }

    func testPendingPairingCannotStartUsingPreviouslySavedConnection() {
        let saved = status(bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"])
        XCTAssertEqual(saved.startBlocker(isBusy: false, hasAccessChanges: false, needsPairing: true), .setup)
    }

    func testConnectionTitleDoesNotClaimAccessDuringTransitionsOrFailure() {
        XCTAssertEqual(status(bridge: "stopped", helper: "starting").connectionTitle, "Connecting…")
        XCTAssertEqual(status(bridge: "stopping", helper: "stopping").connectionTitle, "Stopping…")
        XCTAssertEqual(status(bridge: "faulted", helper: "running").connectionTitle, "Connection needs attention")
        XCTAssertEqual(status(bridge: "observe_only", helper: "running").connectionTitle, "Connected")
    }

    func testAppSelectionCannotPublishOverIncompletePairing() {
        let incomplete = status(bridge: "stopped", helper: "ready", config: "ready", enabled: false)
        XCTAssertEqual(incomplete.accessSaveAction, .setup)
        XCTAssertFalse(incomplete.hasSavedConnection)
        XCTAssertEqual(incomplete.nextSetupSection(hasAccessChanges: true), .setup)
    }

    func testStepChecksRequireSavedChoicesAndCompletedConnection() {
        let saved = status(bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"])
        XCTAssertEqual(saved.setupProgress(for: .setup, needsPairing: false, hasAccessChanges: false), .complete("Saved"))
        XCTAssertEqual(saved.setupProgress(for: .setup, needsPairing: true, hasAccessChanges: false), .needsAction("Finish setup"))
        XCTAssertEqual(saved.setupProgress(for: .access, needsPairing: false, hasAccessChanges: false), .complete("1 app"))
        XCTAssertEqual(saved.setupProgress(for: .access, needsPairing: false, hasAccessChanges: true), .needsAction("Unsaved changes"))
        let empty = status(bridge: "stopped", helper: "ready", config: "ready")
        XCTAssertEqual(empty.setupProgress(for: .access, needsPairing: false, hasAccessChanges: false), .needsAction("Nothing selected"))
    }

    func testPermissionAndStartChecksReflectActualRuntimeState() {
        let missing = status(bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"], permissionsGranted: false)
        XCTAssertEqual(missing.setupProgress(for: .permissions, needsPairing: false, hasAccessChanges: false), .needsAction("Not allowed"))
        XCTAssertEqual(missing.setupProgress(for: .session, needsPairing: false, hasAccessChanges: false), .idle("Off"))
        let ready = status(bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"])
        XCTAssertEqual(ready.setupProgress(for: .permissions, needsPairing: false, hasAccessChanges: false), .complete("Allowed"))
        XCTAssertEqual(ready.setupProgress(for: .session, needsPairing: false, hasAccessChanges: false), .idle("Ready"))
        let active = status(bridge: "observe_only", helper: "running", config: "ready", apps: ["com.apple.TextEdit"])
        XCTAssertEqual(active.setupProgress(for: .session, needsPairing: false, hasAccessChanges: false), .complete("Running"))
    }

    func testFolderOrShellOnlyAccessSkipsGUIPermissionsAndStartsAccess() {
        for (roots, shellEnabled, summary) in [(["/Users/test/Projects"], false, "1 folder"), ([], true, "Shell")] {
            let value = status(
                bridge: "stopped", helper: "ready", config: "ready", permissionsGranted: false,
                fileRoots: roots, shellEnabled: shellEnabled
            )

            XCTAssertNil(value.startBlocker(isBusy: false, hasAccessChanges: false))
            XCTAssertEqual(value.nextSetupSection(hasAccessChanges: false), .session)
            XCTAssertEqual(value.setupProgress(for: .access, needsPairing: false, hasAccessChanges: false), .complete(summary))
            XCTAssertEqual(value.setupProgress(for: .permissions, needsPairing: false, hasAccessChanges: false), .idle("Not needed"))
            XCTAssertEqual(value.setupProgress(for: .session, needsPairing: false, hasAccessChanges: false), .idle("Ready"))
            XCTAssertFalse(value.needsGUIPermissions)
            XCTAssertEqual(value.startActionTitle, "Start Access")
        }
        let appsOnly = status(bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"])
        XCTAssertEqual(appsOnly.startActionTitle, "Start Observe Only")
    }

    func testNothingSavedBlocksStartUntilACapabilityIsSaved() {
        for browserConfigured in [false, true] {
            // Browser control needs a selected app's GUI provider, so a browser alone is not start-ready.
            let value = status(bridge: "stopped", helper: "ready", config: "ready", browserConfigured: browserConfigured)

            XCTAssertEqual(value.startBlocker(isBusy: false, hasAccessChanges: false), .noAccess)
            XCTAssertEqual(DesktopStartBlocker.noAccess.destination, .access)
            XCTAssertEqual(value.nextSetupSection(hasAccessChanges: false), .access)
            XCTAssertEqual(value.setupProgress(for: .access, needsPairing: false, hasAccessChanges: false), .needsAction("Nothing selected"))
            XCTAssertEqual(value.setupProgress(for: .session, needsPairing: false, hasAccessChanges: false), .idle("Off"))
        }
    }

    func testUnsavedAccessDraftsShowUnsavedAndBlockStart() {
        let value = status(bridge: "stopped", helper: "ready", config: "ready", fileRoots: ["/Users/test/Projects"])

        XCTAssertEqual(value.startBlocker(isBusy: false, hasAccessChanges: true), .unsavedAccess)
        XCTAssertEqual(DesktopStartBlocker.unsavedAccess.destination, .access)
        XCTAssertEqual(value.nextSetupSection(hasAccessChanges: true), .access)
        XCTAssertEqual(value.setupProgress(for: .access, needsPairing: false, hasAccessChanges: true), .needsAction("Unsaved changes"))
        XCTAssertEqual(value.accessProgress(for: .folders, hasChanges: true), .needsAction("Unsaved changes"))
        XCTAssertEqual(value.accessProgress(for: .folders, hasChanges: false), .complete("1 folder"))
        XCTAssertEqual(value.accessProgress(for: .shell, hasChanges: false), .idle("Off"))
        XCTAssertEqual(value.accessProgress(for: .applications, hasChanges: false), .idle("None"))
        let shell = status(bridge: "stopped", helper: "ready", config: "ready", shellEnabled: true)
        XCTAssertEqual(shell.accessProgress(for: .shell, hasChanges: false), .complete("Allowed"))
    }

    func testSavedAppsStillRequireGUIPermissionsAlongsideLocalAccess() {
        let value = status(
            bridge: "stopped", helper: "ready", config: "ready", apps: ["com.apple.TextEdit"], permissionsGranted: false,
            fileRoots: ["/Users/test/Projects"], shellEnabled: true
        )

        XCTAssertTrue(value.needsGUIPermissions)
        XCTAssertEqual(value.startBlocker(isBusy: false, hasAccessChanges: false), .permissions)
        XCTAssertEqual(value.nextSetupSection(hasAccessChanges: false), .permissions)
        XCTAssertEqual(value.setupProgress(for: .permissions, needsPairing: false, hasAccessChanges: false), .needsAction("Not allowed"))
        XCTAssertEqual(value.setupProgress(for: .access, needsPairing: false, hasAccessChanges: false), .complete("1 app · 1 folder · Shell"))
        XCTAssertEqual(value.startActionTitle, "Start Access")
    }

    func testPendingShellCommandShowsApprovalStateAndBlocksEmergencyReset() {
        let request = shellRequest()
        let pending = status(
            bridge: "observe_only", helper: "running", config: "ready", apps: ["com.apple.TextEdit"],
            shellEnabled: true, shell: DesktopShellStatus(enabled: true, pending: request)
        )

        XCTAssertEqual(pending.shellApprovalState, .pending(request))
        XCTAssertEqual(pending.shellApprovalState.label, "Shell command waiting for approval")
        XCTAssertTrue(pending.hasBridgeWorkInFlight)
        // Shell commands can change the account, so the connection is never labelled observe-only.
        XCTAssertEqual(pending.accessModeLabel(controlRemainingSeconds: 0), "Apps observe only")
        let shellOnly = status(
            bridge: "observe_only", helper: "running", config: "ready", shellEnabled: true,
            shell: DesktopShellStatus(enabled: true)
        )
        XCTAssertEqual(shellOnly.accessModeLabel(controlRemainingSeconds: 0), "Access on")
        XCTAssertEqual(shellOnly.shellApprovalState, .askEachTime)
        XCTAssertFalse(shellOnly.hasBridgeWorkInFlight)
        let appsOnly = status(bridge: "control", helper: "running", config: "ready", apps: ["com.apple.TextEdit"])
        XCTAssertEqual(appsOnly.accessModeLabel(controlRemainingSeconds: 125), "Control · 2m 5s")
        XCTAssertEqual(appsOnly.shellApprovalState, .off)

        let running = status(
            bridge: "observe_only", helper: "running", config: "ready", shellEnabled: true,
            shell: DesktopShellStatus(enabled: true, autoApproveRemainingSeconds: 250.2, activeRequestID: "shell-2")
        )
        XCTAssertTrue(running.hasBridgeWorkInFlight)
        XCTAssertEqual(running.shellApprovalState, .autoApprove(seconds: 251))
        XCTAssertEqual(running.shellApprovalState.label, "Shell auto-approval · 4m 11s left")
        XCTAssertTrue(running.shell.hasRevocableWork)
        let untilStopped = status(
            bridge: "observe_only", helper: "running", config: "ready", shellEnabled: true,
            shell: DesktopShellStatus(enabled: true, autoApproveUntilRevoked: true)
        )
        XCTAssertEqual(untilStopped.shellApprovalState, .untilRevoked)
        XCTAssertEqual(untilStopped.shellApprovalState.label, "Shell auto-approval until you stop it")
        XCTAssertFalse(shellOnly.shell.hasRevocableWork)
        let stopping = status(
            bridge: "stopping", helper: "stopping", config: "ready", shellEnabled: true,
            shell: DesktopShellStatus(enabled: true, pending: request)
        )
        XCTAssertEqual(stopping.shellApprovalState, .off)
    }

    func testApprovalPreviewEscapesControlAndDirectionalCharacters() {
        let command = "cat notes\u{202E}txt.sh\u{1B}[8m hidden\u{200B}\r\nls\tdone \u{2066}x\u{2069}\u{2028}end"

        XCTAssertEqual(
            desktopSafePreview(command),
            #"cat notes\u{202E}txt.sh\u{1B}[8m hidden\u{200B}\u{D}"# + "\n"
                + #"ls\u{9}done \u{2066}x\u{2069}\u{2028}end"#
        )
        XCTAssertTrue(desktopPreviewEscapes(command))
        XCTAssertEqual(desktopSafePreview("printf 'a\\n'\nls -la ~/Projects"), "printf 'a\\n'\nls -la ~/Projects")
        XCTAssertFalse(desktopPreviewEscapes("printf 'a\\n'\nls -la ~/Projects"))
        let request = shellRequest(command: command, cwd: "/Users/test/\u{202E}stcejorP", agent: "assi\u{200F}stant")
        XCTAssertEqual(request.displayCommand, desktopSafePreview(command))
        XCTAssertEqual(request.displayCwd, #"/Users/test/\u{202E}stcejorP"#)
        XCTAssertEqual(request.displayAgentName, #"assi\u{200F}stant"#)
        XCTAssertEqual(request.command, command, "Execution keeps the original command")
    }

    func testAutoApprovalConfirmationNamesAllLocallyAllowedCallers() {
        let value = status(
            bridge: "observe_only", helper: "running", config: "ready", shellEnabled: true,
            requesterIDs: ["@person:example.org", "@other:example.org"], agentNames: ["assistant", "researcher"]
        )

        let timed = value.shellAutoApprovalScope(.minutes(15))
        XCTAssertTrue(timed.contains("all locally allowed agents and requesters"), timed)
        XCTAssertTrue(timed.contains("for 15 minutes"), timed)
        XCTAssertTrue(timed.contains("assistant, researcher"), timed)
        XCTAssertTrue(timed.contains("@person:example.org, @other:example.org"), timed)
        XCTAssertTrue(timed.contains("your macOS account"), timed)
        let untilStopped = value.shellAutoApprovalScope(.untilStopped)
        XCTAssertTrue(untilStopped.contains("all locally allowed agents and requesters"), untilStopped)
        XCTAssertTrue(untilStopped.contains("until you revoke shell access or stop computer access"), untilStopped)
        XCTAssertEqual(DesktopShellAutoApproval.choices.map(\.title), ["5 Minutes", "15 Minutes", "60 Minutes", "Until I Stop"])
    }

    private func shellRequest(
        command: String = "ls -la", cwd: String = "/Users/test", agent: String = "assistant"
    ) -> DesktopShellRequest {
        DesktopShellRequest(
            requestID: "shell-1", requesterID: "@person:example.org", agentName: agent,
            command: command, cwd: cwd, expiresAtMilliseconds: 1_900_000_000_000
        )
    }

    private func status(
        bridge: String, helper: String, config: String = "missing", apps: [String] = [],
        session: DesktopSessionState = .ready, permissionsGranted: Bool = true, enabled: Bool = true,
        fileRoots: [String] = [], shellEnabled: Bool = false, shell: DesktopShellStatus = DesktopShellStatus(),
        browserConfigured: Bool = false, requesterIDs: [String]? = nil, agentNames: [String]? = nil
    ) -> DesktopStatus {
        let base = DesktopStatus.stopped
        return DesktopStatus(
            config: DesktopConfigStatus(
                state: config, revision: 1, enabled: enabled,
                controllerUserID: nil, controllerDeviceID: nil,
                allowedRequesterIDs: requesterIDs, allowedAgentNames: agentNames, allowedAppIDs: apps,
                fileRoots: fileRoots, shellEnabled: shellEnabled
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
            browser: DesktopBrowserStatus(
                configured: browserConfigured, executablePath: nil, userDataDirectory: nil,
                runtime: "available", extensionState: browserConfigured ? "disconnected" : "disabled",
                reconnectTokenConfigured: false, lastError: nil
            ),
            apps: base.apps, capabilities: base.capabilities, shell: shell
        )
    }
}
