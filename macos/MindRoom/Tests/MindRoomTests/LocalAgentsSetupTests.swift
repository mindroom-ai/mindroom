import XCTest
@testable import MindRoom

final class LocalAgentsSetupTests: XCTestCase {
    func testInstalledRuntimeDoesNotNeedReinstallWhenServiceIsMissing() {
        let setup = LocalAgentsSetupSnapshot(runtimePath: "/example/mindroom", configurationExists: false)
        XCTAssertEqual(setup.progress(for: .install, service: .notInstalled, check: nil), .complete("Installed"))
        XCTAssertEqual(setup.nextStep(service: .notInstalled, check: nil), .configure)
        XCTAssertFalse(setup.canStart(service: .notInstalled))
    }

    func testExistingConfigurationNeedsValidationBeforeRecommendedStart() {
        let setup = LocalAgentsSetupSnapshot(runtimePath: "/example/mindroom", configurationExists: true)
        XCTAssertEqual(setup.nextStep(service: .notInstalled, check: nil), .check)
        XCTAssertEqual(setup.progress(for: .check, service: .notInstalled, check: nil), .idle("Not checked"))
        XCTAssertEqual(setup.nextStep(service: .notInstalled, check: CommandResult(exitCode: 0, output: "6 passed, 0 failed, 0 warnings")), .start)
        XCTAssertTrue(setup.canStart(service: .notInstalled))
    }

    func testStoppedServiceCanUseConfigurationSavedByTerminalInstall() {
        let setup = LocalAgentsSetupSnapshot(runtimePath: "/example/mindroom", configurationExists: false)
        XCTAssertTrue(setup.canStart(service: .stopped))
        XCTAssertFalse(setup.canStart(service: .notInstalled))
    }

    func testDoctorWarningsAndUnrecognizedResultsNeedAttention() {
        let setup = LocalAgentsSetupSnapshot(runtimePath: "/example/mindroom", configurationExists: true)
        for output in ["! OPENAI_API_KEY not set\n6 passed, 0 failed, 1 warning\n", "Check finished"] {
            let result = CommandResult(exitCode: 0, output: output)
            XCTAssertEqual(setup.nextStep(service: .notInstalled, check: result), .check)
            XCTAssertEqual(setup.progress(for: .check, service: .notInstalled, check: result), .needsAction("Needs attention"))
        }
    }

    func testRunningAndStoppedServicesOpenEverydayControls() {
        let setup = LocalAgentsSetupSnapshot(runtimePath: "/example/mindroom", configurationExists: true)
        XCTAssertEqual(setup.nextStep(service: .running, check: nil), .start)
        XCTAssertEqual(setup.nextStep(service: .stopped, check: nil), .start)
        XCTAssertFalse(setup.canStart(service: .unknown))
        XCTAssertEqual(setup.progress(for: .start, service: .running, check: nil), .complete("Running"))
        XCTAssertEqual(setup.progress(for: .check, service: .running, check: nil), .idle("Not checked"))
    }

    func testSnapshotFindsExecutableAndConfigurationIndependently() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        let bin = home.appendingPathComponent(".local/bin")
        let config = home.appendingPathComponent(".mindroom/config.yaml")
        try FileManager.default.createDirectory(at: bin, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: config.deletingLastPathComponent(), withIntermediateDirectories: true)
        let runtime = MindRoomRuntime(homeURL: home, bundleURL: home, environment: ["PATH": ""])
        let executable = bin.appendingPathComponent("mindroom")
        try Data("#!/bin/sh\n".utf8).write(to: executable)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executable.path)
        let before = runtime.localSetupSnapshot()
        XCTAssertEqual(before.runtimePath, executable.path)
        XCTAssertFalse(before.configurationExists)
        try Data("agents: {}\n".utf8).write(to: config)
        let after = runtime.localSetupSnapshot()
        XCTAssertTrue(after.configurationExists)
        XCTAssertNotEqual(before.configurationStamp, after.configurationStamp)
        try Data("SYNTHETIC_TEST=true\n".utf8).write(to: home.appendingPathComponent(".mindroom/.env"))
        XCTAssertNotEqual(after.configurationStamp, runtime.localSetupSnapshot().configurationStamp)
    }

    func testRuntimeFromAnotherReleaseMustBeUpdatedBeforeSetupContinues() {
        let setup = LocalAgentsSetupSnapshot(
            runtimePath: "/example/mindroom", runtimeVersion: "2026.9.378", requiredRuntimeVersion: "2026.9.379", configurationExists: true
        )
        XCTAssertEqual(setup.runtimeUpdateReason, "This app needs MindRoom runtime 2026.9.379, but runtime 2026.9.378 is installed.")
        XCTAssertFalse(setup.runtimeReady)
        XCTAssertEqual(setup.progress(for: .install, service: .running, check: nil), .needsAction("Update needed"))
        XCTAssertEqual(setup.nextStep(service: .running, check: nil), .install)
        XCTAssertEqual(setup.nextStep(service: .pairing, check: nil), .install)
        // Starting an installed service uses the version pinned at service install, not new CLI options.
        XCTAssertTrue(setup.canStart(service: .stopped))
        XCTAssertFalse(setup.canStart(service: .notInstalled))

        let unknown = LocalAgentsSetupSnapshot(runtimePath: "/example/mindroom", requiredRuntimeVersion: "2026.9.379", configurationExists: true)
        XCTAssertEqual(unknown.runtimeUpdateReason, "This app needs MindRoom runtime 2026.9.379, but the installed runtime version is unknown.")
    }

    func testMatchingOrUnpinnedRuntimeIsReady() {
        let matching = LocalAgentsSetupSnapshot(
            runtimePath: "/example/mindroom", runtimeVersion: "2026.9.379", requiredRuntimeVersion: "2026.9.379", configurationExists: true
        )
        let unpinned = LocalAgentsSetupSnapshot(runtimePath: "/example/mindroom", configurationExists: true)
        for setup in [matching, unpinned] {
            XCTAssertNil(setup.runtimeUpdateReason)
            XCTAssertTrue(setup.runtimeReady)
            XCTAssertEqual(setup.progress(for: .install, service: .running, check: nil), .complete("Installed"))
            XCTAssertEqual(setup.nextStep(service: .running, check: nil), .start)
        }
        let missing = LocalAgentsSetupSnapshot(requiredRuntimeVersion: "2026.9.379")
        XCTAssertNil(missing.runtimeUpdateReason)
        XCTAssertEqual(missing.progress(for: .install, service: .runtimeMissing, check: nil), .needsAction("Needed"))
    }

    func testServiceWaitingForPairingLeadsToConnectAccount() {
        let setup = LocalAgentsSetupSnapshot(runtimePath: "/example/mindroom", configurationExists: true)
        XCTAssertEqual(setup.nextStep(service: .pairing, check: nil), .configure)
        XCTAssertEqual(setup.progress(for: .start, service: .pairing, check: nil), .needsAction("Waiting for chat account"))
        XCTAssertFalse(setup.canStart(service: .pairing))
    }
}
