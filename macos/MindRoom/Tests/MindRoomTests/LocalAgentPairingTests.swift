import Combine
import XCTest
@testable import MindRoom

final class LocalAgentPairingTests: XCTestCase {
    func testApprovalIsExtractedOnlyFromACompleteURLLine() {
        let output = "Connect this machine to MindRoom:\n  https://chat.mindroom.chat/connect?code=ABCD-EFGH\nWaiting for approval…\n"
        let approval = LocalAgentPairingApproval.parse(output)
        XCTAssertEqual(approval?.url.absoluteString, "https://chat.mindroom.chat/connect?code=ABCD-EFGH")
        XCTAssertEqual(approval?.code, "ABCD-EFGH")
        for url in ["https://custom.example.org/approve?code=ABCD-EFGH", "http://localhost:8000/pair?code=ABCD-EFGH"] {
            XCTAssertEqual(LocalAgentPairingApproval.parse(url + "\n")?.url.absoluteString, url)
        }
        XCTAssertNil(LocalAgentPairingApproval.parse("  https://chat.mindroom.chat/connect?code=ABCD-EFGH"))
        for line in ["https://chat.mindroom.chat", "https://chat.mindroom.chat/connect?code=ABCD-", "file:///connect?code=ABCD-EFGH", "https://user:password@chat.mindroom.chat/connect?code=ABCD-EFGH"] {
            XCTAssertNil(LocalAgentPairingApproval.parse(line + "\n"), line)
        }
    }

    @MainActor
    func testWaitingPairingStreamsBeforeExitAndCancelReturnsToLocalAgents() async {
        let approvalArrived = expectation(description: "Approval arrived while process is waiting")
        let finished = expectation(description: "Cancelled process finished")
        var sections: [AppSection] = []
        let runner = MindRoomCommandRunner(processRunner: { invocation, process in
            guard invocation.arguments.contains("connect") else {
                return CommandResult(exitCode: 0, output: "MindRoom service: not installed")
            }
            return process.run(MindRoomCommandInvocation(
                executableURL: URL(fileURLWithPath: "/bin/sh"),
                // Stay in this process, with no child holding the output pipe open.
                arguments: ["-c", "printf 'Connect this machine to MindRoom:\\n  https://chat.mindroom.chat/connect?code=ABCD-EFGH\\n'; while :; do :; done"],
                environment: [:]
            ))
        }, showSection: { sections.append($0) })
        let observation = runner.$pairingApproval.compactMap { $0 }.prefix(1).sink { _ in approvalArrived.fulfill() }
        runner.onCommandFinished = { _, _ in finished.fulfill() }
        runner.run(.pairHosted)
        await fulfillment(of: [approvalArrived], timeout: 3)
        XCTAssertTrue(runner.isRunningCommand)
        XCTAssertEqual(runner.pairingApproval?.code, "ABCD-EFGH")
        XCTAssertEqual(sections, [.chat])
        runner.cancelPairing()
        await fulfillment(of: [finished], timeout: 3)
        XCTAssertFalse(runner.isRunningCommand)
        XCTAssertNil(runner.pairingApproval)
        XCTAssertNil(runner.feedback)
        XCTAssertFalse(runner.needsReconnectConfirmation)
        XCTAssertEqual(sections.last, .localAgents)
        withExtendedLifetime(observation) {}
    }

    func testCancellationBeforeLaunchPreventsCommandFromRunning() {
        let process = MindRoomCommandProcess()
        process.cancel()
        let result = process.run(MindRoomCommandInvocation(
            executableURL: URL(fileURLWithPath: "/bin/sh"), arguments: ["-c", "printf should-not-run"], environment: [:]
        ))
        XCTAssertFalse(result.isSuccess)
        XCTAssertEqual(result.output, "")
    }

    @MainActor
    func testOlderRuntimeRefusesPairingWithoutOpeningChat() async {
        let finished = expectation(description: "Unsupported runtime reported")
        var sections: [AppSection] = []
        let runner = MindRoomCommandRunner(processRunner: { _, _ in
            CommandResult(exitCode: 2, output: "No such option: --graceful-cancel")
        }, showSection: { sections.append($0) })
        runner.onCommandFinished = { _, _ in finished.fulfill() }
        runner.run(.pairHosted)
        await fulfillment(of: [finished], timeout: 3)
        XCTAssertEqual(runner.feedback?.result.exitCode, 2)
        XCTAssertTrue(runner.lastOutput.contains("No such option: --graceful-cancel"))
        XCTAssertNil(runner.pairingApproval)
        XCTAssertEqual(sections, [.localAgents])
    }

    @MainActor
    func testLateCancelKeepsCompletedPairingAndSaveFailureVisible() async {
        for exitCode: Int32 in [0, 1, SIGTERM] {
            let finished = expectation(description: "Approved pairing finished with \(exitCode)")
            let output = "Connected.\n\(exitCode == 0 ? "Saved credentials" : "Could not save credentials; recovery exports")"
            let runner = MindRoomCommandRunner(processRunner: { invocation, process in
                guard invocation.arguments.contains("connect") else {
                    return CommandResult(exitCode: 0, output: "MindRoom service: running")
                }
                process.cancel()
                return CommandResult(exitCode: exitCode, output: output)
            }, showSection: { _ in })
            runner.onCommandFinished = { _, _ in finished.fulfill() }
            runner.run(.pairHosted)
            await fulfillment(of: [finished], timeout: 3)
            XCTAssertFalse(runner.pairingCancelled)
            XCTAssertEqual(runner.feedback?.result.exitCode, exitCode)
            XCTAssertEqual(runner.lastOutput, output)
        }
    }

    @MainActor
    func testFailedPairingClearsApprovalAndShowsCLIError() async {
        let finished = expectation(description: "Pairing failed")
        var sections: [AppSection] = []
        let runner = MindRoomCommandRunner(processRunner: { invocation, process in
            guard invocation.arguments.contains("connect") else {
                return CommandResult(exitCode: 0, output: "MindRoom service: not installed")
            }
            return process.run(MindRoomCommandInvocation(
                executableURL: URL(fileURLWithPath: "/bin/sh"),
                arguments: ["-c", "printf 'https://chat.mindroom.chat/connect?code=ABCD-EFGH\\nApproval expired\\n'; exit 1"],
                environment: [:]
            ))
        }, showSection: { sections.append($0) })
        runner.onCommandFinished = { _, _ in finished.fulfill() }
        runner.run(.pairHosted)
        await fulfillment(of: [finished], timeout: 3)
        XCTAssertEqual(sections, [.chat, .localAgents])
        XCTAssertNil(runner.pairingApproval)
        XCTAssertFalse(runner.isPairing)
        XCTAssertFalse(runner.needsReconnectConfirmation)
        XCTAssertEqual(runner.feedback?.result.exitCode, 1)
        XCTAssertTrue(runner.lastOutput.contains("Approval expired"))
    }
}
