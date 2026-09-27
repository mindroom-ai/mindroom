import XCTest
@testable import MindRoom

final class CommandRunnerTests: XCTestCase {
    @MainActor
    func testWebActionsNavigateInsideApp() {
        var sections: [AppSection] = []
        let runner = MindRoomCommandRunner(processRunner: { _ in
            XCTFail("Web navigation must not run a process")
            return CommandResult(exitCode: 1, output: "")
        }, showSection: { sections.append($0) })
        runner.run(.openHostedChat)
        runner.run(.openDashboard)
        XCTAssertEqual(sections, [.chat, .dashboard])
    }
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
    func testPairHostedInvokesConnectWithOpenBrowser() async {
        let completed = expectation(description: "Pairing finished")
        // The runner also refreshes service status on background queues, so record every invocation.
        let recorder = InvocationRecorder()
        let runner = MindRoomCommandRunner(processRunner: { invocation in
            recorder.record(invocation.arguments)
            return CommandResult(exitCode: 0, output: "Paired")
        })
        runner.onCommandFinished = { _, _ in completed.fulfill() }
        runner.run(.pairHosted)
        await fulfillment(of: [completed], timeout: 3)
        XCTAssertTrue(recorder.arguments.contains(["mindroom", "connect", "--open-browser"]))
        XCTAssertEqual(runner.feedback?.title, "Pair Chat Account")
    }
}

private final class InvocationRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [[String]] = []

    func record(_ arguments: [String]) {
        lock.lock()
        defer { lock.unlock() }
        recorded.append(arguments)
    }

    var arguments: [[String]] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }
}
