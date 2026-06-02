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
}
