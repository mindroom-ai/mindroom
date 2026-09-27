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
        XCTAssertEqual(setup.nextStep(service: .notInstalled, check: CommandResult(exitCode: 0, output: "Passed")), .start)
        XCTAssertTrue(setup.canStart(service: .notInstalled))
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
}
