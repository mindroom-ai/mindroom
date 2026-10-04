import Foundation

struct LocalInferenceModel: Identifiable, Equatable {
    let size: String
    let downloadGB: Double
    let minimumMemoryGB: Int
    var id: String { "unsloth/Qwen3.5-\(size)-GGUF:Q4_K_M" }
    var name: String { "Qwen 3.5 \(size)" }

    static let choices = [
        LocalInferenceModel(size: "2B", downloadGB: 1.5, minimumMemoryGB: 8),
        LocalInferenceModel(size: "4B", downloadGB: 2.8, minimumMemoryGB: 16),
        LocalInferenceModel(size: "9B", downloadGB: 5.7, minimumMemoryGB: 24),
    ]

    static func recommended(memoryGB: Int) -> LocalInferenceModel {
        choices.last { $0.minimumMemoryGB <= memoryGB } ?? choices[0]
    }
}

struct InferenceModels: Decodable {
    let data: [Model]
    struct Model: Decodable { let id: String; let status: Status }
    struct Status: Decodable { let value: String; let failed: Bool? }
}

struct InferenceDownloadEvent: Decodable {
    let model: String
    let event: String
    let data: Payload?
    struct Payload: Decodable {
        let progress: [String: Download]?
    }
    struct Download: Decodable { let done: Int64; let total: Int64 }
}

/// The app sets up the engine; launchd owns its lifetime, including after the app quits.
@MainActor
final class LocalInferenceController: ObservableObject {
    nonisolated static let baseURL = URL(string: "http://127.0.0.1:11435")!
    nonisolated static let serviceLabel = "chat.mindroom.inference"
    @Published private(set) var busy = false
    @Published private(set) var progress: Double?
    @Published private(set) var message = "Choose a model to run AI on this Mac without a provider API key."
    @Published private(set) var serverRunning = false
    @Published private(set) var installed = false
    private var setupTask: Task<Void, Never>?
    private var downloadError: String?
    private let label: String
    private let runtime: MindRoomRuntime
    private let home: URL
    private let session: URLSession

    init(runtime: MindRoomRuntime = MindRoomRuntime(),
         session: URLSession = .shared, label: String = LocalInferenceController.serviceLabel) {
        self.label = label
        self.runtime = runtime
        self.home = runtime.homeURL
        self.session = session
    }

    private var directory: URL { home.appendingPathComponent("Library/Application Support/MindRoom/inference") }
    private var plist: URL { home.appendingPathComponent("Library/LaunchAgents/\(label).plist") }
    private var keyFile: URL { directory.appendingPathComponent("api-key") }
    private var domain: String { "gui/\(getuid())" }

    func refresh(updateMessage: Bool = true) async {
        installed = FileManager.default.fileExists(atPath: plist.path)
        guard installed else { serverRunning = false; return }
        serverRunning = (try? await models()) != nil
        if !busy && updateMessage { message = serverRunning ? "Local inference server is running." : "Local inference server is stopped. Start it to use your downloaded models." }
    }

    func setup(model: LocalInferenceModel, finished: @escaping () -> Void) {
        guard !busy else { return }
        busy = true
        progress = nil
        downloadError = nil
        setupTask = Task {
            defer { busy = false; progress = nil; setupTask = nil }
            do {
                message = "Preparing local inference…"
                try await installServer()
                let cached = try await models().data.contains { $0.id == model.id && $0.status.value != "downloading" }
                if !cached {
                    let available = try directory.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
                        .volumeAvailableCapacityForImportantUsage ?? 0
                    guard available > Int64((model.downloadGB + 1) * 1_000_000_000) else {
                        throw SetupError("Free at least \(Int(model.downloadGB + 1)) GB of disk space before downloading this model.")
                    }
                    message = "Downloading \(model.name)…"
                    let updates = Task { await self.watchDownload(model: model) }
                    defer { updates.cancel() }
                    _ = try await request("models", model: model.id)
                    var missing = 0
                    while true {
                        try Task.checkCancellation()
                        if let downloadError { throw SetupError(downloadError) }
                        let entry = try await models().data.first { $0.id == model.id }
                        if let entry {
                            missing = 0
                            if entry.status.failed == true { throw SetupError("Model download failed. Retry or inspect inference.log in the logs folder.") }
                            if entry.status.value != "downloading" { break }
                        } else {
                            missing += 1
                            if missing >= 30 { throw SetupError("The download did not complete. Check your connection and inference.log, then retry.") }
                        }
                        try await Task.sleep(for: .seconds(1))
                    }
                }
                try Task.checkCancellation()
                progress = nil
                message = "Loading \(model.name)…"
                _ = try await request("models/load", model: model.id, timeout: 300)
                for _ in 0..<180 {
                    try Task.checkCancellation()
                    let status = try await models().data.first { $0.id == model.id }?.status
                    if status?.failed == true { throw SetupError("Model loading failed. Check available memory and inference.log.") }
                    if status?.value == "loaded" { break }
                    try await Task.sleep(for: .seconds(1))
                }
                guard try await models().data.contains(where: { $0.id == model.id && $0.status.value == "loaded" }) else {
                    throw SetupError("Model loading timed out. Inspect inference.log before retrying.")
                }
                try Task.checkCancellation()
                message = "Configuring your default model…"
                let key = try String(contentsOf: keyFile, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
                let result = await run(runtime.command(for: .useLocalModel(model.id, key)))
                guard result.isSuccess else { throw SetupError(result.output) }
                message = "\(model.name) is ready. Your default model now runs on this Mac."
                finished()
            } catch is CancellationError {
                _ = await Task { try? await self.request("models/unload", model: model.id) }.value
                message = "Setup cancelled. Retry to resume the download."
            } catch {
                if Task.isCancelled {
                    _ = await Task { try? await self.request("models/unload", model: model.id) }.value
                    message = "Setup cancelled. Retry to resume the download."
                } else { message = error.localizedDescription }
            }
            await refresh(updateMessage: false)
        }
    }

    var isSettingUp: Bool { setupTask != nil }

    func cancel() { setupTask?.cancel() }

    func start() async {
        guard !busy, installed else { return }
        busy = true
        defer { busy = false }
        message = "Starting local inference…"
        do {
            if await run(launchInvocation(["print", "\(domain)/\(label)"])).isSuccess {
                try await launchctl(["kickstart", "-k", "\(domain)/\(label)"])
            } else { try await launchctl(["bootstrap", domain, plist.path]) }
            try await waitForServer()
            message = "Local inference server is running."
        } catch { message = error.localizedDescription }
        serverRunning = (try? await models()) != nil
    }

    func stop() async {
        guard !busy, installed else { return }
        busy = true
        defer { busy = false }
        message = "Stopping local inference…"
        do {
            try await unloadService()
            serverRunning = false
            message = "Local inference stopped. Agents using local models need it started again."
        } catch { message = error.localizedDescription }
    }

    private func installServer() async throws {
        let files = FileManager.default
        try files.createDirectory(at: directory, withIntermediateDirectories: true)
        let build = try String(contentsOf: runtime.bundledInferenceURL.appendingPathComponent("BUILD"), encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let engine = directory.appendingPathComponent("engine-\(build)")
        if !files.fileExists(atPath: engine.appendingPathComponent("llama-server").path) {
            let staging = directory.appendingPathComponent(UUID().uuidString)
            defer { try? files.removeItem(at: staging) }
            try files.copyItem(at: runtime.bundledInferenceURL, to: staging)
            try files.moveItem(at: staging, to: engine)
        }
        if !files.fileExists(atPath: keyFile.path) {
            try Data(UUID().uuidString.utf8).write(to: keyFile, options: .atomic)
            try files.setAttributes([.posixPermissions: 0o600], ofItemAtPath: keyFile.path)
        }
        try files.createDirectory(at: plist.deletingLastPathComponent(), withIntermediateDirectories: true)
        try files.createDirectory(at: runtime.logsDirectoryURL, withIntermediateDirectories: true)
        let properties: [String: Any] = [
            "Label": label,
            "ProgramArguments": [engine.appendingPathComponent("llama-server").path,
                                 "--host", "127.0.0.1", "--port", "11435", "--api-key-file", keyFile.path,
                                 "--models-max", "1", "--offline", "--ctx-size", "8192", "--parallel", "1",
                                 "--n-gpu-layers", "99", "--flash-attn", "on", "--jinja", "--no-mmproj",
                                 "--no-ui", "--sleep-idle-seconds", "300", "--reasoning", "off"],
            "EnvironmentVariables": ["LLAMA_CACHE": directory.appendingPathComponent("models").path,
                                     "LLAMA_ARG_MMPROJ_AUTO": "0"],
            "RunAtLoad": true, "KeepAlive": true, "ThrottleInterval": 10,
            "StandardOutPath": runtime.logsDirectoryURL.appendingPathComponent("inference.log").path,
            "StandardErrorPath": runtime.logsDirectoryURL.appendingPathComponent("inference.log").path,
        ]
        if let data = try? Data(contentsOf: plist),
           let saved = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
           NSDictionary(dictionary: saved).isEqual(to: properties), (try? await models()) != nil {
            installed = true
            serverRunning = true
            return
        }
        try await unloadService()
        try PropertyListSerialization.data(fromPropertyList: properties, format: .xml, options: 0).write(to: plist, options: .atomic)
        try await launchctl(["bootstrap", domain, plist.path])
        installed = true
        try await waitForServer()
        serverRunning = true
    }

    private func unloadService() async throws {
        let target = "\(domain)/\(label)"
        guard await run(launchInvocation(["print", target])).isSuccess else { return }
        try await launchctl(["bootout", target])
        // bootout can return before launchd removes the job; bootstrap must wait.
        for _ in 0..<50 {
            if !(await run(launchInvocation(["print", target]))).isSuccess { return }
            try await Task.sleep(for: .milliseconds(200))
        }
        throw SetupError("The inference service is still stopping. Wait a moment and retry.")
    }

    private func waitForServer() async throws {
        for _ in 0..<30 {
            try Task.checkCancellation()
            if (try? await models()) != nil { return }
            try await Task.sleep(for: .seconds(1))
        }
        throw SetupError("The inference server did not start. Check inference.log and whether port 11435 is available.")
    }

    private func watchDownload(model: LocalInferenceModel) async {
        do {
            var request = URLRequest(url: Self.baseURL.appendingPathComponent("models/sse"), timeoutInterval: 86_400)
            let key = try String(contentsOf: keyFile, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
            request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
            let (bytes, response) = try await session.bytes(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                throw SetupError("Could not track the model download. Retry setup.")
            }
            for try await line in bytes.lines {
                try Task.checkCancellation()
                guard line.hasPrefix("data:"),
                      let event = try? JSONDecoder().decode(InferenceDownloadEvent.self, from: Data(line.dropFirst(5).utf8)),
                      event.model == model.id else { continue }
                if event.event == "download_failed" { downloadError = "Model download failed. Check your connection and inference.log, then retry." }
                if event.event == "download_finished" { return }
                if let downloads = event.data?.progress?.values {
                    let total = downloads.reduce(Int64(0)) { $0 + $1.total }
                    let done = downloads.reduce(Int64(0)) { $0 + $1.done }
                    progress = total > 0 ? Double(done) / Double(total) : nil
                    if total > 0 {
                        message = "Downloading \(model.name): \(ByteCountFormatter.string(fromByteCount: done, countStyle: .file)) of \(ByteCountFormatter.string(fromByteCount: total, countStyle: .file))"
                    }
                }
            }
        } catch {
            if !Task.isCancelled { downloadError = error.localizedDescription }
        }
    }

    private func models() async throws -> InferenceModels {
        try JSONDecoder().decode(InferenceModels.self, from: await request("models"))
    }

    private func request(_ path: String, model: String? = nil, timeout: TimeInterval = 15) async throws -> Data {
        var request = URLRequest(url: Self.baseURL.appendingPathComponent(path), timeoutInterval: timeout)
        let key = try String(contentsOf: keyFile, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        if let model {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode(["model": model])
        }
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse, (200..<300).contains(response.statusCode) else {
            throw SetupError("Inference server error: \(String(decoding: data, as: UTF8.self))")
        }
        return data
    }

    private func launchInvocation(_ arguments: [String]) -> MindRoomCommandInvocation {
        MindRoomCommandInvocation(executableURL: URL(fileURLWithPath: "/bin/launchctl"), arguments: arguments,
                                  environment: runtime.commandEnvironment())
    }

    private func launchctl(_ arguments: [String]) async throws {
        let result = await run(launchInvocation(arguments))
        guard result.isSuccess else { throw SetupError(result.output) }
    }

    private func run(_ invocation: MindRoomCommandInvocation) async -> CommandResult {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .utility).async {
                continuation.resume(returning: MindRoomCommandProcess().run(invocation))
            }
        }
    }

    private struct SetupError: LocalizedError {
        let text: String
        init(_ text: String) { self.text = text }
        var errorDescription: String? { text }
    }
}
