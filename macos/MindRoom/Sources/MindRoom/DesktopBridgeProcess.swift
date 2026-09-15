import Darwin
import Foundation

enum DesktopBridgeProcessError: LocalizedError {
    case helperMissing
    case notRunning
    case malformedResponse
    case outputTooLarge
    case timedOut
    case helper(DesktopBridgeErrorPayload)

    var errorDescription: String? {
        switch self {
        case .helperMissing: "The packaged Desktop Helper is missing."
        case .notRunning: "The Desktop Helper is not running."
        case .malformedResponse: "The Desktop Helper returned an invalid response."
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
    @Published private(set) var stderrTail = ""
    var onExit: (() -> Void)?

    private let runtime: MindRoomRuntime
    private var process: Process?
    private var input: FileHandle?
    private var outputBuffer = Data()
    private var pending: [String: CheckedContinuation<Response, Error>] = [:]
    private let decoder = JSONDecoder()
    private var isShuttingDown = false
    private var shutdownCompletion: (() -> Void)?
    private var terminateFallback: DispatchWorkItem?
    private var killFallback: DispatchWorkItem?

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
        child.executableURL = invocation.executableURL
        child.arguments = invocation.arguments
        child.environment = invocation.environment
        child.standardInput = stdinPipe
        child.standardOutput = stdoutPipe
        child.standardError = stderrPipe
        stdoutPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            Task { @MainActor in self?.receive(data) }
        }
        stderrPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            Task { @MainActor in self?.receiveStderr(data) }
        }
        child.terminationHandler = { [weak self] _ in
            Task { @MainActor in self?.didExit() }
        }
        process = child
        input = stdinPipe.fileHandleForWriting
        do {
            try child.run()
        } catch {
            process = nil
            input = nil
            throw error
        }
    }

    func request(
        action: String,
        parameters: [String: Any] = [:],
        timeout: Duration = .seconds(35)
    ) async throws -> Response {
        try launchIfNeeded()
        guard let input else { throw DesktopBridgeProcessError.notRunning }
        let requestID = UUID().uuidString.lowercased()
        let payload: [String: Any] = [
            "v": desktopBridgeProtocolVersion,
            "request_id": requestID,
            "action": action,
            "parameters": parameters,
        ]
        let body = try JSONSerialization.data(withJSONObject: payload)
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                pending[requestID] = continuation
                do {
                    try input.write(contentsOf: body + Data([0x0A]))
                } catch {
                    pending.removeValue(forKey: requestID)
                    continuation.resume(throwing: error)
                    return
                }
                Task { @MainActor [weak self] in
                    try? await Task.sleep(for: timeout)
                    self?.timeout(requestID)
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
        input?.closeFile()
        input = nil
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

    private func receive(_ data: Data) {
        guard !data.isEmpty else { return }
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

    private func receiveStderr(_ data: Data) {
        guard let text = String(data: data, encoding: .utf8), !text.isEmpty else { return }
        stderrTail = String((stderrTail + text).suffix(16_384))
    }

    private func timeout(_ requestID: String) {
        pending.removeValue(forKey: requestID)?.resume(throwing: DesktopBridgeProcessError.timedOut)
    }

    private func didExit() {
        let completion = shutdownCompletion
        shutdownCompletion = nil
        terminateFallback?.cancel()
        terminateFallback = nil
        killFallback?.cancel()
        killFallback = nil
        isShuttingDown = false
        process = nil
        input = nil
        outputBuffer.removeAll(keepingCapacity: true)
        status = .stopped
        failAll(DesktopBridgeProcessError.notRunning)
        onExit?()
        completion?()
    }

    private func terminateImmediately() {
        input?.closeFile()
        input = nil
        process?.terminate()
    }

    private func failAll(_ error: Error) {
        let continuations = Array(pending.values)
        pending.removeAll()
        continuations.forEach { $0.resume(throwing: error) }
    }
}
