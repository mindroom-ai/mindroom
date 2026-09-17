import XCTest
@testable import MindRoom

final class CommandRunnerTests: XCTestCase {
    @MainActor
    func testCommandCompletionPublishesFailureWithoutBlockingNextAction() async {
        let completed = expectation(description: "Command finished")
        let runner = MindRoomCommandRunner(processRunner: { _ in
            CommandResult(exitCode: 1, output: "Provider credentials missing")
        })
        runner.onCommandFinished = { _, _ in completed.fulfill() }
        runner.run(.startService)
        XCTAssertTrue(runner.isRunningCommand)
        await fulfillment(of: [completed], timeout: 3)
        XCTAssertFalse(runner.isRunningCommand)
        XCTAssertEqual(runner.feedback?.title, MindRoomCommand.startService.title)
        XCTAssertEqual(runner.feedback?.result.exitCode, 1)
        XCTAssertEqual(runner.lastOutput, "Provider credentials missing")
    }

    @MainActor
    func testPairingFeedbackDoesNotRetainPairCode() async throws {
        let completed = expectation(description: "Pairing finished")
        let pairCode = "test-pair-code-must-be-discarded"
        let runner = MindRoomCommandRunner(processRunner: { _ in
            CommandResult(exitCode: 0, output: "Paired")
        })
        runner.onCommandFinished = { _, _ in completed.fulfill() }
        runner.run(.pairHosted(pairCode: pairCode))
        await fulfillment(of: [completed], timeout: 3)
        let feedback = try XCTUnwrap(runner.feedback)
        XCTAssertFalse(String(reflecting: feedback).contains(pairCode))
        XCTAssertEqual(feedback.title, "Pair Chat Account")
    }
}
