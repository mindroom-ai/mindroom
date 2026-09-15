import Foundation
import XCTest
@testable import MindRoom

@MainActor
final class DesktopBridgeProtocolTests: XCTestCase {
    private static let completeStatusData = """
    {
      "config":{"state":"ready","revision":2,"enabled":true,"controller_user_id":"@controller:example.org","controller_device_id":"CLOUD","allowed_requester_ids":["@me:example.org"],"allowed_agent_names":["assistant"],"allowed_app_ids":["com.example.Editor"]},
      "pairing":{"state":"paired","homeserver":"https://example.org","user_id":"@me:example.org","device_id":"LOCAL","controller_fingerprint":"key"},
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

    func testHydratesPersistedBrowserSettingsBeforeSaving() throws {
        let status = try JSONDecoder().decode(DesktopStatus.self, from: Self.completeStatusData)
        let store = DesktopControlStore()

        store.hydrateConfiguration(from: status)

        XCTAssertTrue(store.browserEnabled)
        XCTAssertEqual(store.browserExecutable, "/Applications/Browser.app/Contents/MacOS/Browser")
        XCTAssertEqual(store.browserProfile, "/Users/test/Library/Application Support/Browser")
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
            "/Applications/MindRoom.app/Contents/Helpers/MindRoom Desktop Helper.app/Contents/MacOS/MindRoom Desktop Helper"
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
}
