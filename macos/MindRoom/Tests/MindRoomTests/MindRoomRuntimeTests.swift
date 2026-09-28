import XCTest
@testable import MindRoom

final class MindRoomRuntimeTests: XCTestCase {
    func testDoctorAndPairingUseInstalledServiceConfiguration() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        let config = home.appendingPathComponent("terminal-project/agents.yaml")
        let storage = home.appendingPathComponent("agent-data")
        let plist = home.appendingPathComponent("Library/LaunchAgents/chat.mindroom.local.plist")
        try FileManager.default.createDirectory(at: config.deletingLastPathComponent(), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: plist.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("agents: {}\n".utf8).write(to: config)
        let data = try PropertyListSerialization.data(fromPropertyList: [
            "Label": "chat.mindroom.local",
            "EnvironmentVariables": ["MINDROOM_CONFIG_PATH": config.path, "MINDROOM_STORAGE_PATH": storage.path],
        ], format: .xml, options: 0)
        try data.write(to: plist)
        let runtime = MindRoomRuntime(homeURL: home, bundleURL: home, environment: [:])
        XCTAssertEqual(runtime.command(for: .checkSetup).arguments, ["mindroom", "doctor", "--config", config.path])
        XCTAssertEqual(runtime.command(for: .checkSetup).environment["MINDROOM_STORAGE_PATH"], storage.path)
        XCTAssertEqual(runtime.command(for: .pairHosted).environment["MINDROOM_CONFIG_PATH"], config.path)
        XCTAssertTrue(runtime.localSetupSnapshot().configurationExists)
        // Computer access keeps its independent configuration binding.
        XCTAssertEqual(runtime.desktopHelperInvocation().arguments, ["--config", home.appendingPathComponent(".mindroom/config.yaml").path])
    }

    func testDefaultPathsUseHomeMindroom() {
        let runtime = MindRoomRuntime(
            homeURL: URL(fileURLWithPath: "/Users/example", isDirectory: true),
            bundleURL: URL(fileURLWithPath: "/Applications/MindRoom.app", isDirectory: true),
            environment: ["PATH": "/usr/bin:/bin"]
        )

        XCTAssertEqual(runtime.configDirectoryURL.path, "/Users/example/.mindroom")
        XCTAssertEqual(runtime.configPathURL.path, "/Users/example/.mindroom/config.yaml")
        XCTAssertEqual(runtime.envPathURL.path, "/Users/example/.mindroom/.env")
        XCTAssertEqual(runtime.logsDirectoryURL.path, "/Users/example/Library/Logs/mindroom")
    }

    func testRuntimeInstallUsesBundledUVAndDoesNotRedirectMindRoomConfig() {
        let runtime = MindRoomRuntime(
            homeURL: URL(fileURLWithPath: "/Users/example", isDirectory: true),
            bundleURL: URL(fileURLWithPath: "/Applications/MindRoom.app", isDirectory: true),
            environment: ["PATH": "/usr/bin:/bin"]
        )

        let command = runtime.command(for: .installRuntime)
        XCTAssertEqual(command.executableURL.path, "/Applications/MindRoom.app/Contents/Resources/bin/uv")
        XCTAssertEqual(command.arguments, ["tool", "install", "--managed-python", "--python", "3.13", "mindroom"])
        XCTAssertNil(command.environment["MINDROOM_CONFIG_PATH"])
        XCTAssertNil(command.environment["MINDROOM_STORAGE_PATH"])
        XCTAssertEqual(command.environment["UV_NO_PROGRESS"], "1")
        XCTAssertTrue(command.environment["PATH"]?.hasPrefix("/Users/example/.local/bin:/Applications/MindRoom.app/Contents/Resources/bin:") == true)
    }

    func testRuntimeUpdateForcesLatestMindRoomInstall() {
        let runtime = MindRoomRuntime(
            homeURL: URL(fileURLWithPath: "/Users/example", isDirectory: true),
            bundleURL: URL(fileURLWithPath: "/Applications/MindRoom.app", isDirectory: true),
            environment: ["PATH": "/usr/bin:/bin"]
        )

        let command = runtime.command(for: .updateRuntime)
        XCTAssertEqual(command.arguments, ["tool", "install", "--managed-python", "--python", "3.13", "--force", "mindroom"])
    }

    func testServiceInstallUsesMindRoomServiceInstallNoConfirm() {
        let runtime = MindRoomRuntime(
            homeURL: URL(fileURLWithPath: "/Users/example", isDirectory: true),
            bundleURL: URL(fileURLWithPath: "/Applications/MindRoom.app", isDirectory: true),
            environment: ["PATH": "/usr/bin:/bin"]
        )

        let command = runtime.command(for: .installService)
        XCTAssertEqual(command.executableURL.path, "/usr/bin/env")
        XCTAssertEqual(command.arguments, ["mindroom", "service", "install", "--no-confirm"])
        XCTAssertEqual(command.environment["MINDROOM_CONFIG_PATH"], "/Users/example/.mindroom/config.yaml")
    }

    func testOnlyReconnectForcesPairingAnAlreadyConnectedMac() {
        let runtime = MindRoomRuntime(
            homeURL: URL(fileURLWithPath: "/Users/example", isDirectory: true),
            bundleURL: URL(fileURLWithPath: "/Applications/MindRoom.app", isDirectory: true),
            environment: ["PATH": "/usr/bin:/bin"]
        )

        XCTAssertEqual(runtime.command(for: .pairHosted).arguments, ["mindroom", "connect"])
        XCTAssertEqual(runtime.command(for: .reconnectHosted).arguments, ["mindroom", "connect", "--force"])
        XCTAssertEqual(runtime.command(for: .reconnectHosted).environment["MINDROOM_CONFIG_PATH"], "/Users/example/.mindroom/config.yaml")
    }

    func testHostedConfigCommandUsesPublicProfileWithoutPrompts() {
        let runtime = MindRoomRuntime(
            homeURL: URL(fileURLWithPath: "/Users/example", isDirectory: true),
            bundleURL: URL(fileURLWithPath: "/Applications/MindRoom.app", isDirectory: true),
            environment: ["PATH": "/usr/bin:/bin"]
        )

        let command = runtime.command(for: .initializeHostedConfig)
        XCTAssertEqual(
            command.arguments,
            ["mindroom", "config", "init", "--path", "/Users/example/.mindroom/config.yaml", "--matrix-server", "mindroom.chat", "--no-input"]
        )
    }

    func testSelfHostedConfigCommandRunsWithoutPrompts() {
        let runtime = MindRoomRuntime(
            homeURL: URL(fileURLWithPath: "/Users/example", isDirectory: true),
            bundleURL: URL(fileURLWithPath: "/Applications/MindRoom.app", isDirectory: true),
            environment: ["PATH": "/usr/bin:/bin"]
        )

        let command = runtime.command(for: .initializeSelfHostedConfig)
        XCTAssertEqual(
            command.arguments,
            ["mindroom", "config", "init", "--path", "/Users/example/.mindroom/config.yaml", "--matrix-server", "self-hosted", "--no-input"]
        )
    }
}
