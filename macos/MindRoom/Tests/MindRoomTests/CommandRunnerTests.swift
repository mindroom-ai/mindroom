import XCTest
import Combine
@testable import MindRoom

final class CommandRunnerTests: XCTestCase {
    @MainActor
    func testWarningOnlyDoctorFeedbackNeedsAttentionWithoutProcessFailure() async {
        let finished = expectation(description: "Doctor finished")
        let runner = MindRoomCommandRunner(processRunner: { _, _ in
            CommandResult(exitCode: 0, output: "! OPENAI_API_KEY not set\n6 passed, 0 failed, 1 warning")
        })
        runner.onCommandFinished = { _, _ in finished.fulfill() }
        runner.run(.checkSetup)
        await fulfillment(of: [finished], timeout: 2)
        XCTAssertEqual(runner.feedback?.result.isSuccess, true)
        XCTAssertEqual(runner.feedback?.needsAttention, true)
        XCTAssertEqual(runner.feedback?.statusLabel, "needs attention")
        XCTAssertEqual(runner.feedback?.statusSymbol, "exclamationmark.triangle")
    }

    @MainActor
    func testStatusRefreshObservesConfigurationEditsDuringProcess() async throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        let config = home.appendingPathComponent(".mindroom/config.yaml")
        try FileManager.default.createDirectory(at: config.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("agents: {}\n".utf8).write(to: config)
        let refreshed = expectation(description: "Status finished")
        let runner = MindRoomCommandRunner(runtime: MindRoomRuntime(homeURL: home, bundleURL: home, environment: [:]), processRunner: { _, _ in
            try? FileManager.default.removeItem(at: config)
            return CommandResult(exitCode: 0, output: "MindRoom service: not installed")
        })
        let observation = runner.$hasRefreshedStatus.filter { $0 }.sink { _ in refreshed.fulfill() }
        runner.refreshStatus()
        await fulfillment(of: [refreshed], timeout: 2)
        XCTAssertFalse(runner.localSetup.configurationExists)
        withExtendedLifetime(observation) {}
    }

    @MainActor
    func testFailedStatusCannotReportRunningFromIncidentalOutput() async {
        let refreshed = expectation(description: "Status refreshed")
        let runner = MindRoomCommandRunner(processRunner: { _, _ in
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
            processRunner: { invocation, _ in
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
            processRunner: { invocation, _ in
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
        let runner = MindRoomCommandRunner(processRunner: { _, _ in
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
        let runner = MindRoomCommandRunner(processRunner: { _, _ in
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
        let runner = MindRoomCommandRunner(processRunner: { _, _ in
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
        let runner = MindRoomCommandRunner(processRunner: { _, _ in
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
    func testPairHostedKeepsApprovalInsideApp() async {
        let completed = expectation(description: "Pairing finished")
        // The runner also refreshes service status on background queues, so record every invocation.
        let recorder = InvocationRecorder()
        let runner = MindRoomCommandRunner(processRunner: { invocation, _ in
            recorder.record(invocation.arguments)
            return CommandResult(exitCode: 0, output: "Paired")
        }, showSection: { _ in })
        runner.onCommandFinished = { _, _ in completed.fulfill() }
        runner.run(.pairHosted)
        await fulfillment(of: [completed], timeout: 3)
        XCTAssertTrue(recorder.arguments.contains(["mindroom", "connect", "--graceful-cancel"]))
        XCTAssertEqual(runner.feedback?.title, "Pair Chat Account")
        XCTAssertFalse(runner.needsReconnectConfirmation)
    }

    @MainActor
    func testAlreadyConnectedPairingAsksInsteadOfReportingFailure() async {
        let completed = expectation(description: "Pairing refused")
        let runner = MindRoomCommandRunner(processRunner: { invocation, _ in
            invocation.arguments.contains("connect")
                ? CommandResult(exitCode: MindRoomCommand.alreadyConnectedExitCode, output: "This machine is already connected.")
                : CommandResult(exitCode: 0, output: "MindRoom service: running (pid 123)")
        }, showSection: { _ in })
        runner.onCommandFinished = { _, _ in completed.fulfill() }
        runner.run(.pairHosted)
        await fulfillment(of: [completed], timeout: 3)
        XCTAssertTrue(runner.needsReconnectConfirmation)
        XCTAssertNil(runner.feedback)
        XCTAssertFalse(runner.isRunningCommand)
    }

    @MainActor
    func testReconnectHostedForcesPairing() async {
        let completed = expectation(description: "Reconnect finished")
        let recorder = InvocationRecorder()
        let runner = MindRoomCommandRunner(processRunner: { invocation, _ in
            recorder.record(invocation.arguments)
            return CommandResult(exitCode: 0, output: "Paired")
        }, showSection: { _ in })
        runner.onCommandFinished = { _, _ in completed.fulfill() }
        runner.run(.reconnectHosted)
        await fulfillment(of: [completed], timeout: 3)
        XCTAssertTrue(recorder.arguments.contains(["mindroom", "connect", "--graceful-cancel", "--force"]))
        XCTAssertEqual(runner.feedback?.title, "Reconnect Chat Account")
        XCTAssertEqual(runner.feedback?.result.isSuccess, true)
        XCTAssertFalse(runner.needsReconnectConfirmation)
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
