import XCTest
import Combine
@testable import MindRoom

final class CommandRunnerTests: XCTestCase {
    @MainActor
    func testFailedStatusCannotReportRunningFromIncidentalOutput() async {
        let refreshed = expectation(description: "Status refreshed")
        let runner = MindRoomCommandRunner(processRunner: { _ in
            CommandResult(exitCode: 1, output: "Last known service: running; status check failed")
        })
        let observation = runner.$hasRefreshedStatus.filter { $0 }.sink { _ in refreshed.fulfill() }
        runner.refreshStatus()
        await fulfillment(of: [refreshed], timeout: 2)
        XCTAssertEqual(runner.serviceStatus.state, .unknown)
        withExtendedLifetime(observation) {}
    }

    @MainActor
    func testEditingConfigurationInvalidatesSuccessfulSetupCheckOnRefresh() async throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        let config = home.appendingPathComponent(".mindroom/config.yaml")
        try FileManager.default.createDirectory(at: config.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("agents: {}\n".utf8).write(to: config)
        let executable = home.appendingPathComponent(".local/bin/mindroom")
        try FileManager.default.createDirectory(at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("#!/bin/sh\n".utf8).write(to: executable)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executable.path)
        let checked = expectation(description: "Setup checked")
        let refreshed = expectation(description: "Configuration refreshed")
        let runner = MindRoomCommandRunner(
            runtime: MindRoomRuntime(homeURL: home, bundleURL: home, environment: [:]),
            processRunner: { invocation in
                if invocation.arguments.contains("doctor") {
                    return CommandResult(exitCode: 0, output: "6 passed, 0 failed")
                }
                return CommandResult(exitCode: 0, output: "MindRoom service: not installed")
            }
        )
        runner.onCommandFinished = { _, _ in checked.fulfill() }
        runner.run(.checkSetup)
        await fulfillment(of: [checked], timeout: 2)
        XCTAssertEqual(runner.setupCheck?.isSuccess, true)
        try FileManager.default.setAttributes([.modificationDate: Date(timeIntervalSince1970: 100)], ofItemAtPath: config.path)
        let observation = runner.$isRefreshingStatus.dropFirst().filter { !$0 }.sink { _ in refreshed.fulfill() }
        runner.refreshStatus()
        await fulfillment(of: [refreshed], timeout: 2)
        // Completion publishes all snapshot fields in the same main-queue turn.
        await Task.yield()
        XCTAssertNil(runner.setupCheck)
        XCTAssertEqual(runner.localSetup.nextStep(service: .notInstalled, check: runner.setupCheck), .check)
        withExtendedLifetime(observation) {}
    }

    @MainActor
    func testConfigurationEditDuringDoctorDoesNotProduceGreenCheck() async throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        let config = home.appendingPathComponent(".mindroom/config.yaml")
        try FileManager.default.createDirectory(at: config.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("agents: {}\n".utf8).write(to: config)
        let checked = expectation(description: "Setup checked")
        let runner = MindRoomCommandRunner(
            runtime: MindRoomRuntime(homeURL: home, bundleURL: home, environment: [:]),
            processRunner: { invocation in
                if invocation.arguments.contains("doctor") {
                    try? FileManager.default.setAttributes([.modificationDate: Date(timeIntervalSince1970: 100)], ofItemAtPath: config.path)
                    return CommandResult(exitCode: 0, output: "6 passed, 0 failed")
                }
                return CommandResult(exitCode: 0, output: "MindRoom service: not installed")
            }
        )
        runner.onCommandFinished = { _, _ in checked.fulfill() }
        runner.run(.checkSetup)
        await fulfillment(of: [checked], timeout: 2)
        XCTAssertEqual(runner.setupCheck?.isSuccess, false)
        XCTAssertEqual(runner.feedback?.result.isSuccess, false)
    }

    @MainActor
    func testRefreshRequestedDuringAnotherRefreshIsNotLost() async {
        let firstStarted = expectation(description: "First status started")
        let refreshed = expectation(description: "Fresh status started")
        let releaseFirst = DispatchSemaphore(value: 0)
        let calls = StatusRefreshCalls()
        let runner = MindRoomCommandRunner(processRunner: { _ in
            if calls.next() == 1 {
                firstStarted.fulfill()
                _ = releaseFirst.wait(timeout: .now() + 3)
                return CommandResult(exitCode: 0, output: "MindRoom service: not installed")
            }
            refreshed.fulfill()
            return CommandResult(exitCode: 0, output: "MindRoom service: running (pid 123)")
        })
        runner.refreshStatus()
        await fulfillment(of: [firstStarted], timeout: 2)
        runner.refreshStatus()
        releaseFirst.signal()
        await fulfillment(of: [refreshed], timeout: 2)
        for _ in 0..<100 where runner.serviceStatus.state != .running {
            try? await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertEqual(runner.serviceStatus.state, .running)
    }

    @MainActor
    func testPeriodicPollingDoesNotStarveSlowStatusRead() async {
        let started = expectation(description: "Status started")
        let published = expectation(description: "Slow status published")
        let release = DispatchSemaphore(value: 0)
        let calls = StatusRefreshCalls()
        let runner = MindRoomCommandRunner(processRunner: { _ in
            let call = calls.next()
            XCTAssertEqual(call, 1)
            if call == 1 {
                started.fulfill()
                _ = release.wait(timeout: .now() + 3)
            }
            return CommandResult(exitCode: 0, output: "MindRoom service: running (pid 123)")
        })
        let observation = runner.$hasRefreshedStatus.filter { $0 }.sink { _ in published.fulfill() }
        runner.refreshStatus()
        await fulfillment(of: [started], timeout: 2)
        runner.refreshStatus(queueIfBusy: false)
        runner.refreshStatus(queueIfBusy: false)
        release.signal()
        await fulfillment(of: [published], timeout: 2)
        XCTAssertEqual(runner.serviceStatus.state, .running)
        XCTAssertFalse(runner.isRefreshingStatus)
        withExtendedLifetime(observation) {}
    }

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

private final class StatusRefreshCalls: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0
    func next() -> Int {
        lock.lock()
        defer { lock.unlock() }
        count += 1
        return count
    }
}
