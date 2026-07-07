import XCTest
@testable import MindRoom

final class ServiceStatusTests: XCTestCase {
    func testParsesRunningServiceStatus() {
        let status = MindRoomServiceStatus.parse("MindRoom service: running (pid 12345)")

        XCTAssertEqual(status.state, .running)
        XCTAssertEqual(status.message, "MindRoom is running")
    }

    func testParsesStoppedServiceStatus() {
        let status = MindRoomServiceStatus.parse("MindRoom service: installed but not running")

        XCTAssertEqual(status.state, .stopped)
        XCTAssertEqual(status.message, "MindRoom is installed but stopped")
    }

    func testParsesNotInstalledServiceStatus() {
        let status = MindRoomServiceStatus.parse("MindRoom service: not installed")

        XCTAssertEqual(status.state, .notInstalled)
        XCTAssertEqual(status.message, "MindRoom service is not installed")
    }

    func testParsesMissingRuntimeStatus() {
        let status = MindRoomServiceStatus.parse("env: mindroom: No such file or directory")

        XCTAssertEqual(status.state, .runtimeMissing)
        XCTAssertEqual(status.message, "MindRoom runtime is not installed")
    }

    func testParsesUnknownStatusWithTrimmedOutput() {
        let status = MindRoomServiceStatus.parse("\nUnexpected output\n")

        XCTAssertEqual(status.state, .unknown)
        XCTAssertEqual(status.message, "Unexpected output")
    }

    func testParsesUnknownStatusWithCollapsedAndTruncatedOutput() {
        let status = MindRoomServiceStatus.parse(
            String(repeating: "Unexpected output line\n", count: 20)
        )

        XCTAssertEqual(status.state, .unknown)
        XCTAssertLessThanOrEqual(status.message.count, 200)
        XCTAssertFalse(status.message.contains("\n"))
    }
}

final class MindRoomCommandTests: XCTestCase {
    func testPairCodeIsUppercasedAndTrimmed() {
        let command = MindRoomCommand.pairHosted(pairCode: " abcd-efgh ")

        XCTAssertEqual(command.runtimeAction, .pairHosted(pairCode: "ABCD-EFGH"))
    }

    func testMenuCommandsExposeTitles() {
        XCTAssertEqual(MindRoomCommand.installRuntime.title, "Install MindRoom Runtime")
        XCTAssertEqual(MindRoomCommand.openDashboard.title, "Open Dashboard")
        XCTAssertEqual(MindRoomCommand.openConfigFolder.title, "Open Config Folder")
        XCTAssertEqual(MindRoomCommand.openLogsFolder.title, "Open Logs Folder")
    }

    func testEveryUserRunCommandExposesASuccessMessage() {
        let commands: [MindRoomCommand] = [
            .installRuntime, .updateRuntime, .installService, .startService, .stopService,
            .restartService, .initializeHostedConfig, .initializeSelfHostedConfig,
            .localStackSetup, .pairHosted(pairCode: "ABCD-EFGH"),
        ]
        for command in commands {
            XCTAssertNotNil(command.successMessage, "\(command.title) has no success message")
        }
        XCTAssertNil(MindRoomCommand.serviceStatus.successMessage)
        XCTAssertNil(MindRoomCommand.openDashboard.successMessage)
    }
}

final class CommandResultTests: XCTestCase {
    func testCondensedOutputCollapsesBlankLinesAndWhitespace() {
        let result = CommandResult(exitCode: 1, output: "  Error: bad config  \n\n\n  second line \n")

        XCTAssertEqual(result.condensedOutput, "Error: bad config\nsecond line")
    }

    func testCondensedOutputTruncatesLongOutput() {
        let result = CommandResult(exitCode: 1, output: String(repeating: "x", count: 2000))

        XCTAssertEqual(result.condensedOutput.count, 800)
        XCTAssertTrue(result.condensedOutput.hasSuffix("..."))
    }
}
