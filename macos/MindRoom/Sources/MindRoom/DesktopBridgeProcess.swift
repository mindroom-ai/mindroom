import Darwin
import Foundation

enum DesktopBridgeProcessError: LocalizedError {
    case helperMissing
    case notRunning
    case malformedResponse
    case requestTooLarge
    case tooManyRequests
    case outputTooLarge
    case timedOut
    case helper(DesktopBridgeErrorPayload)

    var errorDescription: String? {
        switch self {
        case .helperMissing: "The packaged Desktop Helper is missing."
        case .notRunning: "The Desktop Helper is not running."
        case .malformedResponse: "The Desktop Helper returned an invalid response."
        case .requestTooLarge: "The Desktop Helper request is larger than 65,536 bytes."
        case .tooManyRequests: "The Desktop Helper has too many pending requests."
        case .outputTooLarge: "The Desktop Helper returned an oversized response."
        case .timedOut: "The Desktop Helper did not respond in time."
        case let .helper(error): error.message
        }
    }
}

@MainActor
final class DesktopBridgeProcess: ObservableObject {
    typealias Response = [String: Any]

    @Published private(set) var status = DesktopStatus.stopped
    var onExit: (() -> Void)?

    private let runtime: MindRoomRuntime
    private var process: Process?
    private var input: FileHandle?
    private var outputBuffer = Data()
    private var pending: [String: CheckedContinuation<Response, Error>] = [:]
    private let inputWriterQueue = DispatchQueue(label: "MindRoom.DesktopBridgeProcess.stdin")
    private var queuedInputWrites = 0
    private let decoder = JSONDecoder()
    private var isShuttingDown = false
    private var shutdownCompletion: (() -> Void)?
    private var terminateFallback: DispatchWorkItem?
    private var killFallback: DispatchWorkItem?
    private var stdoutHandle: FileHandle?
    private var stderrHandle: FileHandle?
    private var activeProcessID: UUID?

    init(runtime: MindRoomRuntime = MindRoomRuntime()) {
        self.runtime = runtime
    }

    func launchIfNeeded() throws {
        guard process == nil, !isShuttingDown else {
            if input == nil { throw DesktopBridgeProcessError.notRunning }
            return
        }
        let invocation = runtime.desktopHelperInvocation()
        guard FileManager.default.isExecutableFile(atPath: invocation.executableURL.path) else {
            throw DesktopBridgeProcessError.helperMissing
        }
        let child = Process()
        let stdinPipe = Pipe()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        // A helper exit must turn a pending pipe write into an error, not terminate the app.
        guard fcntl(stdinPipe.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1) != -1 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        child.executableURL = invocation.executableURL
        child.arguments = invocation.arguments
        child.environment = invocation.environment
        child.standardInput = stdinPipe
        child.standardOutput = stdoutPipe
        child.standardError = stderrPipe
        let processID = UUID()
        child.terminationHandler = { [weak self] _ in
            Task { @MainActor in self?.didExit(processID: processID) }
        }
        process = child
        activeProcessID = processID
        input = stdinPipe.fileHandleForWriting
        do {
            try child.run()
            startReaders(
                stdout: stdoutPipe.fileHandleForReading,
                stderr: stderrPipe.fileHandleForReading,
                processID: processID
            )
        } catch {
            enqueueInputClose()
            process = nil
            activeProcessID = nil
            throw error
        }
    }

    private func startReaders(stdout: FileHandle, stderr: FileHandle, processID: UUID) {
        stdoutHandle = stdout
        stderrHandle = stderr
        stdout.readabilityHandler = { [weak self] handle in
            do {
                guard let data = try handle.read(upToCount: 65_536), !data.isEmpty else {
                    handle.readabilityHandler = nil
                    return
                }
                DispatchQueue.main.sync {
                    MainActor.assumeIsolated {
                        self?.receive(data, processID: processID)
                    }
                }
            } catch {
                handle.readabilityHandler = nil
                DispatchQueue.main.sync {
                    MainActor.assumeIsolated {
                        self?.protocolReadFailed(processID: processID)
                    }
                }
            }
        }
        stderr.readabilityHandler = { handle in
            do {
                guard let data = try handle.read(upToCount: 65_536), !data.isEmpty else {
                    handle.readabilityHandler = nil
                    return
                }
            } catch {
                handle.readabilityHandler = nil
            }
        }
    }

    func request(
        action: String,
        parameters: [String: Any] = [:],
        timeout: Duration = .seconds(35)
    ) async throws -> Response {
        let requestID = UUID().uuidString.lowercased()
        let payload: [String: Any] = [
            "v": desktopBridgeProtocolVersion,
            "request_id": requestID,
            "action": action,
            "parameters": parameters,
        ]
        var record = try JSONSerialization.data(withJSONObject: payload)
        record.append(0x0A)
        guard record.count <= desktopBridgeMaximumRequestBytes else {
            throw DesktopBridgeProcessError.requestTooLarge
        }
        let requestRecord = record
        try launchIfNeeded()
        guard let input, let processID = activeProcessID else {
            throw DesktopBridgeProcessError.notRunning
        }
        guard pending.count < desktopBridgeMaximumPendingRequests,
              queuedInputWrites < desktopBridgeMaximumPendingRequests else {
            throw DesktopBridgeProcessError.tooManyRequests
        }
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                pending[requestID] = continuation
                Task { @MainActor [weak self] in
                    try? await Task.sleep(for: timeout)
                    self?.timeout(requestID)
                }
                if Task.isCancelled {
                    self.timeout(requestID)
                    return
                }
                queuedInputWrites += 1
                inputWriterQueue.async { [weak self] in
                    let succeeded: Bool
                    do {
                        try input.write(contentsOf: requestRecord)
                        succeeded = true
                    } catch {
                        succeeded = false
                    }
                    Task { @MainActor [weak self] in
                        self?.inputWriteFinished(
                            requestID: requestID,
                            processID: processID,
                            succeeded: succeeded
                        )
                    }
                }
            }
        } onCancel: {
            Task { @MainActor [weak self] in self?.timeout(requestID) }
        }
    }

    @discardableResult
    func shutdown(
        gracePeriod: TimeInterval = 120,
        forcedTerminationPeriod: TimeInterval = 5,
        completion: @escaping () -> Void
    ) -> Bool {
        guard let child = process else { return false }
        guard !isShuttingDown else { return true }
        isShuttingDown = true
        shutdownCompletion = completion
        enqueueInputClose()
        let terminateFallback = DispatchWorkItem { [weak self, weak child] in
            guard let self, let child, self.process === child, child.isRunning else { return }
            child.terminate()
            let killFallback = DispatchWorkItem { [weak self, weak child] in
                guard let self, let child, self.process === child, child.isRunning else { return }
                kill(child.processIdentifier, SIGKILL)
            }
            self.killFallback = killFallback
            DispatchQueue.main.asyncAfter(deadline: .now() + forcedTerminationPeriod, execute: killFallback)
        }
        self.terminateFallback = terminateFallback
        DispatchQueue.main.asyncAfter(deadline: .now() + gracePeriod, execute: terminateFallback)
        return true
    }

    private func receive(_ data: Data, processID: UUID) {
        guard activeProcessID == processID, !data.isEmpty else { return }
        outputBuffer.append(data)
        if outputBuffer.count > desktopBridgeMaximumOutputBytes,
           !outputBuffer.contains(0x0A) {
            failAll(DesktopBridgeProcessError.outputTooLarge)
            terminateImmediately()
            return
        }
        while let newline = outputBuffer.firstIndex(of: 0x0A) {
            let line = outputBuffer[..<newline]
            outputBuffer.removeSubrange(...newline)
            guard line.count <= desktopBridgeMaximumOutputBytes else {
                failAll(DesktopBridgeProcessError.outputTooLarge)
                terminateImmediately()
                return
            }
            decode(Data(line))
        }
    }

    @discardableResult
    func decode(_ data: Data) -> DesktopBridgeDecodeDisposition {
        guard
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            isDesktopBridgeProtocolVersion(object["v"]),
            let type = object["type"] as? String
        else {
            failAll(DesktopBridgeProcessError.malformedResponse)
            terminateImmediately()
            return .protocolFailure
        }
        if type == "status", let statusObject = object["status"] {
            return updateStatus(statusObject) ? .accepted : .protocolFailure
        }
        if type == "hello" {
            return .accepted
        }
        guard
            type == "response",
            let requestID = object["request_id"] as? String,
            let ok = object["ok"] as? Bool
        else {
            failAll(DesktopBridgeProcessError.malformedResponse)
            terminateImmediately()
            return .protocolFailure
        }
        guard let continuation = pending.removeValue(forKey: requestID) else { return .ignoredExpiredResponse }
        if ok, let result = object["result"] as? Response {
            if let statusObject = result["status"], !updateStatus(statusObject) {
                continuation.resume(throwing: DesktopBridgeProcessError.malformedResponse)
                return .protocolFailure
            }
            continuation.resume(returning: result)
        } else if let errorObject = object["error"],
                  let errorData = try? JSONSerialization.data(withJSONObject: errorObject),
                  let error = try? decoder.decode(DesktopBridgeErrorPayload.self, from: errorData) {
            continuation.resume(throwing: DesktopBridgeProcessError.helper(error))
        } else {
            continuation.resume(throwing: DesktopBridgeProcessError.malformedResponse)
            failAll(DesktopBridgeProcessError.malformedResponse)
            terminateImmediately()
            return .protocolFailure
        }
        return .accepted
    }

    private func updateStatus(_ object: Any) -> Bool {
        guard
            let data = try? JSONSerialization.data(withJSONObject: object),
            let decoded = try? decoder.decode(DesktopStatus.self, from: data)
        else {
            failAll(DesktopBridgeProcessError.malformedResponse)
            terminateImmediately()
            return false
        }
        status = decoded
        return true
    }

    private func timeout(_ requestID: String) {
        pending.removeValue(forKey: requestID)?.resume(throwing: DesktopBridgeProcessError.timedOut)
    }

    private func didExit(processID: UUID) {
        guard activeProcessID == processID else { return }
        let completion = shutdownCompletion
        shutdownCompletion = nil
        terminateFallback?.cancel()
        terminateFallback = nil
        killFallback?.cancel()
        killFallback = nil
        stdoutHandle?.readabilityHandler = nil
        stdoutHandle = nil
        stderrHandle?.readabilityHandler = nil
        stderrHandle = nil
        activeProcessID = nil
        isShuttingDown = false
        process = nil
        enqueueInputClose()
        outputBuffer.removeAll(keepingCapacity: true)
        status = .stopped
        failAll(DesktopBridgeProcessError.notRunning)
        onExit?()
        completion?()
    }

    private func terminateImmediately() {
        enqueueInputClose()
        process?.terminate()
    }

    private func enqueueInputClose() {
        guard let input else { return }
        self.input = nil
        inputWriterQueue.async {
            input.closeFile()
        }
    }

    private func inputWriteFinished(requestID: String, processID: UUID, succeeded: Bool) {
        queuedInputWrites = max(0, queuedInputWrites - 1)
        guard activeProcessID == processID, !succeeded else { return }
        pending.removeValue(forKey: requestID)?.resume(throwing: DesktopBridgeProcessError.notRunning)
    }

    private func protocolReadFailed(processID: UUID) {
        guard activeProcessID == processID, process?.isRunning == true else { return }
        failAll(DesktopBridgeProcessError.malformedResponse)
        terminateImmediately()
    }

    private func failAll(_ error: Error) {
        let continuations = Array(pending.values)
        pending.removeAll()
        continuations.forEach { $0.resume(throwing: error) }
    }
}
