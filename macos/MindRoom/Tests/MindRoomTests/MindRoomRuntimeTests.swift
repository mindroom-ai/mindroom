import XCTest
@testable import MindRoom

final class MindRoomRuntimeTests: XCTestCase {
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
    }

    func testHostedConfigCommandUsesPublicProfile() {
        let runtime = MindRoomRuntime(
            homeURL: URL(fileURLWithPath: "/Users/example", isDirectory: true),
            bundleURL: URL(fileURLWithPath: "/Applications/MindRoom.app", isDirectory: true),
            environment: ["PATH": "/usr/bin:/bin"]
        )

        let command = runtime.command(for: .initializeHostedConfig)
        XCTAssertEqual(command.arguments, ["mindroom", "config", "init", "--matrix-server", "mindroom.chat"])
    }
}
