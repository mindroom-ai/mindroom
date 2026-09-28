import Combine
import Foundation
import XCTest
@testable import MindRoom

@MainActor
final class DesktopBridgeProtocolTests: XCTestCase {
    #if arch(arm64)
    private static let helperExecutablePath =
        "Contents/Helpers/arm64/MindRoom Desktop Helper.app/Contents/MacOS/MindRoom Desktop Helper"
    #else
    private static let helperExecutablePath =
        "Contents/Helpers/x86_64/MindRoom Desktop Helper.app/Contents/MacOS/MindRoom Desktop Helper"
    #endif
    private static let completeStatusData = """
    {
      "config":{"state":"ready","revision":2,"enabled":true,"controller_user_id":"@controller:example.org","controller_device_id":"CLOUD","allowed_requester_ids":["@me:example.org"],"allowed_agent_names":["assistant"],"allowed_app_ids":["com.example.Editor"]},
      "pairing":{"state":"paired","session_state":"ready","homeserver":"https://example.org","user_id":"@me:example.org","device_id":"LOCAL","controller_fingerprint":"key"},
      "helper":{"state":"running","version":"1.2.3"},
      "bridge":{"state":"observe_only","active_action":null,"last_error":null},
      "authority":{"control_available":false,"lease_remaining_seconds":0,"lease_expires_at_ms":null,"emergency_stop_latched":false},
      "permissions":{"accessibility":{"state":"granted","can_request":true,"recovery":null},"screen_recording":{"state":"missing","can_request":true,"recovery":"Open Settings"}},
      "browser":{"configured":true,"runtime":"available","extension":"disconnected","reconnect_token_configured":false,"executable_path":"/Applications/Browser.app/Contents/MacOS/Browser","user_data_dir":"/Users/test/Library/Application Support/Browser","last_error":null},
      "apps":[{"id":"com.example.Editor","name":"Editor","installed":true,"running":false}],
      "capabilities":["observe","control","browser"]
    }
    """.data(using: .utf8)!

    func testDecodesCompleteRedactedStatus() throws {
        let status = try JSONDecoder().decode(DesktopStatus.self, from: Self.completeStatusData)
        XCTAssertEqual(status.bridge.state, "observe_only")
        XCTAssertFalse(status.authority.controlAvailable)
        XCTAssertEqual(status.apps.first?.id, "com.example.Editor")
        XCTAssertEqual(status.browser.executablePath, "/Applications/Browser.app/Contents/MacOS/Browser")
        XCTAssertEqual(status.browser.userDataDirectory, "/Users/test/Library/Application Support/Browser")
    }

    func testStatusWithoutFolderAndShellFieldsDecodesThemAsOff() throws {
        let status = try JSONDecoder().decode(DesktopStatus.self, from: Self.completeStatusData)

        XCTAssertEqual(status.config.fileRoots, [])
        XCTAssertFalse(status.config.shellEnabled)
        XCTAssertEqual(status.shell, DesktopShellStatus())
    }

    func testDecodesFolderRootsPendingShellCommandAndHandles() throws {
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: Self.completeStatusData) as? [String: Any])
        var config = try XCTUnwrap(object["config"] as? [String: Any])
        config["file_roots"] = ["/Users/test/Projects"]
        config["shell_enabled"] = true
        object["config"] = config
        object["shell"] = [
            "enabled": true,
            "pending": [
                "request_id": "shell-7", "requester_id": "@me:example.org", "agent_name": "assistant",
                "command": "ls -la", "cwd": "/Users/test", "expires_at_ms": 1_900_000_000_000,
            ],
            "auto_approve_remaining_seconds": 0.0,
            "auto_approve_until_revoked": false,
            "active_request_id": NSNull(),
            "handles": [[
                "handle": "handle-1", "requester_id": "@me:example.org", "agent_name": "assistant",
                "command_preview": "sleep 100", "elapsed_seconds": 12.5, "state": "running",
            ]],
        ]

        let status = try JSONDecoder().decode(DesktopStatus.self, from: JSONSerialization.data(withJSONObject: object))

        XCTAssertEqual(status.config.fileRoots, ["/Users/test/Projects"])
        XCTAssertTrue(status.config.shellEnabled)
        XCTAssertEqual(status.shell.pending, DesktopShellRequest(
            requestID: "shell-7", requesterID: "@me:example.org", agentName: "assistant",
            command: "ls -la", cwd: "/Users/test", expiresAtMilliseconds: 1_900_000_000_000
        ))
        XCTAssertEqual(status.shell.handles, [DesktopShellHandle(
            handle: "handle-1", requesterID: "@me:example.org", agentName: "assistant",
            commandPreview: "sleep 100", elapsedSeconds: 12.5, state: "running"
        )])
        XCTAssertNil(status.shell.activeRequestID)
        let reencoded = try JSONDecoder().decode(DesktopStatus.self, from: JSONEncoder().encode(status))
        XCTAssertEqual(reencoded, status)
    }

    func testHydratesPersistedBrowserSettingsBeforeSaving() throws {
        let status = try JSONDecoder().decode(DesktopStatus.self, from: Self.completeStatusData)
        let store = DesktopControlStore()

        store.hydrateConfiguration(from: status)

        XCTAssertTrue(store.browserEnabled)
        XCTAssertEqual(store.browserExecutable, "/Applications/Browser.app/Contents/MacOS/Browser")
        XCTAssertEqual(store.browserProfile, "/Users/test/Library/Application Support/Browser")
    }

    func testAppSelectionStopsBeforeSavingAndRetainsDraftWhenStopFails() async throws {
        for stopFails in [false, true] {
            let manager = FileManager.default
            let root = manager.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
            defer { try? manager.removeItem(at: root) }
            let bundle = root.appendingPathComponent("MindRoom.app", isDirectory: true)
            let executable = bundle.appendingPathComponent(Self.helperExecutablePath)
            try manager.createDirectory(at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
            let requestsURL = root.appendingPathComponent("requests.jsonl")
            var savedStatus = try XCTUnwrap(JSONSerialization.jsonObject(with: Self.completeStatusData) as? [String: Any])
            var config = try XCTUnwrap(savedStatus["config"] as? [String: Any])
            config["revision"] = 3
            config["allowed_app_ids"] = ["com.example.Other"]
            savedStatus["config"] = config
            savedStatus["bridge"] = ["state": "stopped"]
            let savedJSON = String(decoding: try JSONSerialization.data(withJSONObject: savedStatus), as: UTF8.self)
            let stopResponse = stopFails
                ? #""ok":false,"error":{"code":"busy","message":"Stop failed","retryable":true}"#
                : #""ok":true,"result":{}"#
            let script = """
            #!/bin/sh
            while IFS= read -r line; do
              printf '%s\\n' "$line" >> '\(requestsURL.path)'
              request_id=$(printf '%s\\n' "$line" | sed -n 's/.*"request_id"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p')
              case "$line" in
                *'"set_allowed_apps"'*) printf '{"v":1,"type":"response","request_id":"%s","ok":true,"result":{"status":%s}}\\n' "$request_id" '\(savedJSON)' ;;
                *) printf '{"v":1,"type":"response","request_id":"%s",%s}\\n' "$request_id" '\(stopResponse)' ;;
              esac
            done
            """
            try script.write(to: executable, atomically: true, encoding: .utf8)
            try manager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executable.path)
            let helper = DesktopBridgeProcess(runtime: MindRoomRuntime(homeURL: root, bundleURL: bundle, environment: [:]))
            let store = DesktopControlStore(helper: helper)
            let ready = expectation(description: "store received active connection")
            let statusSubscription = store.$status.filter { $0.bridge.state == "observe_only" }.prefix(1)
                .sink { _ in ready.fulfill() }
            let initialStatus = String(decoding: Self.completeStatusData, as: UTF8.self)
            _ = helper.decode(Data("{\"v\":1,\"type\":\"status\",\"sequence\":1,\"status\":\(initialStatus)}".utf8))
            await fulfillment(of: [ready], timeout: 2)
            store.selectedAppIDs = ["com.example.Other"]
            // Unrelated form drafts must never be sent with an app-only save.
            store.controllerUserID = "@unsaved:example.org"
            store.browserProfile = "/unsaved"
            let completed = expectation(description: "app save completed")
            let busySubscription = store.$isBusy.dropFirst().filter { !$0 }.prefix(1)
                .sink { _ in completed.fulfill() }

            store.saveAllowedApplications()
            await fulfillment(of: [completed], timeout: 3)

            let records = try String(contentsOf: requestsURL, encoding: .utf8).split(separator: "\n").map {
                try XCTUnwrap(JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any])
            }
            XCTAssertEqual(records.compactMap { $0["action"] as? String }, stopFails ? ["stop"] : ["stop", "set_allowed_apps"])
            XCTAssertEqual(store.selectedAppIDs, ["com.example.Other"])
            XCTAssertEqual(store.browserProfile, "/unsaved")
            if stopFails {
                XCTAssertEqual(store.errorMessage, "Stop failed")
                XCTAssertTrue(store.hasAppSelectionChanges)
            } else {
                let parameters = try XCTUnwrap(records.last?["parameters"] as? [String: Any])
                XCTAssertEqual(Set(parameters.keys), ["expected_revision", "allowed_app_ids"])
                XCTAssertEqual(parameters["expected_revision"] as? Int, 2)
                XCTAssertEqual(parameters["allowed_app_ids"] as? [String], ["com.example.Other"])
                XCTAssertEqual(store.status.bridge.state, "stopped")
                XCTAssertFalse(store.hasAppSelectionChanges)
                XCTAssertNil(store.errorMessage)
            }
            let stopped = expectation(description: "fixture helper stopped")
            XCTAssertTrue(helper.shutdown(gracePeriod: 1, forcedTerminationPeriod: 1) { stopped.fulfill() })
            await fulfillment(of: [stopped], timeout: 3)
            withExtendedLifetime((statusSubscription, busySubscription)) {}
        }
    }

    func testHydratesSavedSessionWithoutOverwritingLaterLoginEdits() throws {
        let status = try JSONDecoder().decode(DesktopStatus.self, from: Self.completeStatusData)
        let store = DesktopControlStore()

        store.hydrateConfiguration(from: status)

        XCTAssertEqual(store.homeserver, "https://example.org")
        XCTAssertEqual(store.matrixUserID, "@me:example.org")
        store.homeserver = "https://other.example.org"
        store.matrixUserID = ""
        store.hydrateConfiguration(from: status)
        XCTAssertEqual(store.homeserver, "https://other.example.org")
        XCTAssertEqual(store.matrixUserID, "")
    }

    func testSavedSessionDoesNotOverwritePreparedLoginIdentity() throws {
        let status = try JSONDecoder().decode(DesktopStatus.self, from: Self.completeStatusData)
        let store = DesktopControlStore()
        store.homeserver = "https://other.example.org"
        store.matrixUserID = "@other:other.example.org"

        store.hydrateConfiguration(from: status)

        XCTAssertEqual(store.homeserver, "https://other.example.org")
        XCTAssertEqual(store.matrixUserID, "@other:other.example.org")
    }

    func testProtocolVersionRejectsBooleanAndFloatingPointValues() {
        XCTAssertTrue(isDesktopBridgeProtocolVersion(1))
        XCTAssertFalse(isDesktopBridgeProtocolVersion(true))
        XCTAssertFalse(isDesktopBridgeProtocolVersion(1.0))
    }

    func testRuntimeBuildsFixedNestedHelperInvocation() {
        let runtime = MindRoomRuntime(
            homeURL: URL(fileURLWithPath: "/Users/test"),
            bundleURL: URL(fileURLWithPath: "/Applications/MindRoom.app"),
            environment: [:]
        )
        let invocation = runtime.desktopHelperInvocation()
        XCTAssertEqual(
            invocation.executableURL.path,
            "/Applications/MindRoom.app/" + Self.helperExecutablePath
        )
        XCTAssertEqual(
            invocation.arguments,
            ["--config", "/Users/test/.mindroom/config.yaml"]
        )
    }

    func testLateCorrelatedResponseDoesNotTerminateHealthyHelper() {
        let runtime = MindRoomRuntime(
            homeURL: URL(fileURLWithPath: "/Users/test"),
            bundleURL: URL(fileURLWithPath: "/Applications/MindRoom.app"),
            environment: [:]
        )
        let helper = DesktopBridgeProcess(runtime: runtime)
        let disposition = helper.decode(
            #"{"v":1,"type":"response","request_id":"expired","ok":true,"result":{}}"#.data(using: .utf8)!
        )
        XCTAssertEqual(disposition, .ignoredExpiredResponse)
    }

    func testOversizedRequestIsRejectedBeforeLaunch() async {
        let helper = DesktopBridgeProcess()

        do {
            _ = try await helper.request(
                action: "configure",
                parameters: ["payload": String(repeating: "x", count: desktopBridgeMaximumRequestBytes)]
            )
            XCTFail("Oversized request unexpectedly succeeded")
        } catch DesktopBridgeProcessError.requestTooLarge {
            // Expected.
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testBlockedHelperInputDoesNotBlockMainActor() async throws {
        let fileManager = FileManager.default
        let root = fileManager.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? fileManager.removeItem(at: root) }
        let bundle = root.appendingPathComponent("MindRoom.app", isDirectory: true)
        let executable = bundle.appendingPathComponent(
            Self.helperExecutablePath
        )
        try fileManager.createDirectory(at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
        let script = """
        #!/bin/sh
        exec /usr/bin/yes '{"v":1,"type":"hello"}'
        """
        try script.write(to: executable, atomically: true, encoding: .utf8)
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executable.path)
        let runtime = MindRoomRuntime(homeURL: root, bundleURL: bundle, environment: [:])
        let helper = DesktopBridgeProcess(runtime: runtime)
        try helper.launchIfNeeded()
        let parameters = ["payload": String(repeating: "x", count: 60_000)]
        let requests = (0 ..< desktopBridgeMaximumPendingRequests).map { _ in
            Task { try await helper.request(action: "configure", parameters: parameters, timeout: .seconds(10)) }
        }
        for _ in 0 ..< desktopBridgeMaximumPendingRequests * 2 {
            await Task.yield()
        }
        let responsive = expectation(description: "main actor remained responsive while helper input was blocked")
        DispatchQueue.main.async { responsive.fulfill() }

        await fulfillment(of: [responsive], timeout: 1)

        do {
            _ = try await helper.request(action: "configure", parameters: parameters, timeout: .seconds(1))
            XCTFail("Request beyond the pending-write limit unexpectedly succeeded")
        } catch DesktopBridgeProcessError.tooManyRequests {
            // Expected.
        } catch {
            XCTFail("Unexpected error: \(error)")
        }

        let stopped = expectation(description: "blocked fixture helper stopped")
        XCTAssertTrue(helper.shutdown(gracePeriod: 0.1, forcedTerminationPeriod: 1) { stopped.fulfill() })
        await fulfillment(of: [stopped], timeout: 3)
        for request in requests {
            request.cancel()
            _ = await request.result
        }
    }

    func testFinalResponseResolvesBeforeImmediateHelperExit() async throws {
        let fileManager = FileManager.default
        let root = fileManager.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? fileManager.removeItem(at: root) }
        let bundle = root.appendingPathComponent("MindRoom.app", isDirectory: true)
        let executable = bundle.appendingPathComponent(
            Self.helperExecutablePath
        )
        try fileManager.createDirectory(at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
        let script = """
        #!/bin/sh
        IFS= read -r line || exit 1
        request_id=$(printf '%s\\n' "$line" | sed -n 's/.*"request_id"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p')
        test -n "$request_id" || exit 2
        printf '{"v":1,"type":"response","request_id":"%s","ok":true,"result":{"completed":true}}\\n' "$request_id"
        """
        try script.write(to: executable, atomically: true, encoding: .utf8)
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executable.path)
        let runtime = MindRoomRuntime(homeURL: root, bundleURL: bundle, environment: [:])
        let helper = DesktopBridgeProcess(runtime: runtime)

        for iteration in 0 ..< 12 {
            let exited = expectation(description: "fixture helper exited after response \(iteration)")
            helper.onExit = { exited.fulfill() }

            let result = try await helper.request(action: "status", timeout: .seconds(3))

            XCTAssertEqual(result["completed"] as? Bool, true)
            await fulfillment(of: [exited], timeout: 3)
        }
        helper.onExit = nil
    }

    func testOrderedReaderHydratesBrowserOnlyAfterLiveStatus() async throws {
        let fileManager = FileManager.default
        let root = fileManager.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? fileManager.removeItem(at: root) }
        let bundle = root.appendingPathComponent("MindRoom.app", isDirectory: true)
        let executable = bundle.appendingPathComponent(
            Self.helperExecutablePath
        )
        try fileManager.createDirectory(at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
        let statusObject = try JSONSerialization.jsonObject(with: Self.completeStatusData)
        let compactStatus = try JSONSerialization.data(withJSONObject: statusObject)
        let statusJSON = String(decoding: compactStatus, as: UTF8.self)
        let script = """
        #!/bin/sh
        printf '%s' '{"v":1,"type":"status","sequence":1,"status":'
        sleep 0.05
        printf '%s' '\(statusJSON)'
        sleep 0.05
        printf '}\\n'
        cat >/dev/null
        """
        try script.write(to: executable, atomically: true, encoding: .utf8)
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executable.path)
        let runtime = MindRoomRuntime(homeURL: root, bundleURL: bundle, environment: [:])
        let helper = DesktopBridgeProcess(runtime: runtime)
        let store = DesktopControlStore(helper: helper)
        XCTAssertFalse(store.canEditBrowserConfiguration)
        let received = expectation(description: "fragmented helper status was decoded in order")
        let subscription = store.$status.dropFirst().sink { status in
            if status.helper.state == "running" {
                received.fulfill()
            }
        }

        try helper.launchIfNeeded()
        await fulfillment(of: [received], timeout: 2)

        XCTAssertTrue(store.canEditBrowserConfiguration)
        XCTAssertTrue(store.browserEnabled)
        XCTAssertEqual(store.browserExecutable, "/Applications/Browser.app/Contents/MacOS/Browser")
        let stopped = expectation(description: "fixture helper stopped")
        XCTAssertTrue(helper.shutdown(gracePeriod: 1, forcedTerminationPeriod: 1) { stopped.fulfill() })
        await fulfillment(of: [stopped], timeout: 3)
        withExtendedLifetime(subscription) {}
    }
}
