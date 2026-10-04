import XCTest
@testable import MindRoom

final class LocalInferenceTests: XCTestCase {
    func testRecommendationsLeaveRoomForOperatingSystemAndAgents() {
        XCTAssertEqual(LocalInferenceModel.recommended(memoryGB: 8).size, "2B")
        XCTAssertEqual(LocalInferenceModel.recommended(memoryGB: 16).size, "4B")
        XCTAssertEqual(LocalInferenceModel.recommended(memoryGB: 24).size, "9B")
    }

    func testDownloadProgressUsesActualServerResponseShape() throws {
        // b11146 reports download progress over SSE, separate from GET /models.
        let data = Data(#"{"model":"test","event":"download_progress","data":{"progress":{"file":{"done":256,"total":1024}}}}"#.utf8)
        let event = try JSONDecoder().decode(InferenceDownloadEvent.self, from: data)
        XCTAssertEqual(event.data?.progress?["file"]?.done, 256)
        XCTAssertEqual(event.data?.progress?["file"]?.total, 1024)
        let loading = Data(#"{"data":[{"id":"test","status":{"value":"loading","progress":{"stages":["text_model"],"value":0.5}}}]}"#.utf8)
        XCTAssertEqual(try JSONDecoder().decode(InferenceModels.self, from: loading).data[0].status.value, "loading")
    }

    func testConfigurationInvocationUsesExplicitServiceConfigAndLocalEndpoint() {
        let runtime = MindRoomRuntime(homeURL: URL(fileURLWithPath: "/Users/test"), environment: [:])
        let invocation = runtime.command(for: .useLocalModel("test-model", "test-key"))
        XCTAssertEqual(invocation.arguments, ["mindroom", "config", "use-local-model", "--path", "/Users/test/.mindroom/config.yaml",
                                               "--model", "test-model", "--base-url", "http://127.0.0.1:11435/v1", "--api-key", "test-key"])
    }

    /// Opt-in smoke test with the actual packaged engine, cached model, CLI, and launchd.
    @MainActor
    func testPackagedEngineSetupAndBackgroundLifecycle() async throws {
        let environment = ProcessInfo.processInfo.environment
        guard let resources = environment["MINDROOM_INFERENCE_SMOKE_RESOURCES"],
              let cli = environment["MINDROOM_INFERENCE_SMOKE_CLI"],
              let cache = environment["MINDROOM_INFERENCE_SMOKE_CACHE"] else {
            throw XCTSkip("Set the smoke-test resource, CLI, and cache paths to exercise actual local inference.")
        }
        let files = FileManager.default
        let home = files.temporaryDirectory.appendingPathComponent("mindroom-inference-\(UUID().uuidString)")
        let label = "chat.mindroom.inference.smoke.\(UUID().uuidString)"
        let bundle = home.appendingPathComponent("MindRoom.app")
        let bundledEngine = bundle.appendingPathComponent("Contents/Resources/llama.cpp")
        let executable = home.appendingPathComponent(".local/bin/mindroom")
        let directory = home.appendingPathComponent("Library/Application Support/MindRoom/inference")
        try files.createDirectory(at: bundledEngine.deletingLastPathComponent(), withIntermediateDirectories: true)
        try files.copyItem(at: URL(fileURLWithPath: resources), to: bundledEngine)
        try files.createDirectory(at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
        try files.createSymbolicLink(at: executable, withDestinationURL: URL(fileURLWithPath: cli))
        try files.createDirectory(at: directory, withIntermediateDirectories: true)
        let models = directory.appendingPathComponent("models")
        try files.createDirectory(at: models, withIntermediateDirectories: true)
        let repository = "models--unsloth--Qwen3.5-4B-GGUF"
        try files.createSymbolicLink(at: models.appendingPathComponent(repository),
                                    withDestinationURL: URL(fileURLWithPath: cache).appendingPathComponent(repository))
        let config = home.appendingPathComponent(".mindroom/config.yaml")
        try files.createDirectory(at: config.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("models: {}\nagents: {}\n".utf8).write(to: config)
        let runtime = MindRoomRuntime(homeURL: home, bundleURL: bundle, environment: environment)
        let controller = LocalInferenceController(runtime: runtime, label: label)
        defer {
            let invocation = MindRoomCommandInvocation(executableURL: URL(fileURLWithPath: "/bin/launchctl"),
                arguments: ["bootout", "gui/\(getuid())/\(label)"], environment: environment)
            _ = MindRoomCommandProcess().run(invocation)
            try? files.removeItem(at: home)
        }
        var finished = false
        controller.setup(model: LocalInferenceModel.choices[1]) { finished = true }
        for _ in 0..<300 {
            if !controller.busy { break }
            try await Task.sleep(for: .seconds(1))
        }
        XCTAssertTrue(finished, controller.message)
        XCTAssertTrue(controller.serverRunning, controller.message)
        XCTAssertTrue(try String(contentsOf: config).contains("llama_cpp"))
        let key = try String(contentsOf: directory.appendingPathComponent("api-key"))
        var request = URLRequest(url: LocalInferenceController.baseURL.appendingPathComponent("v1/chat/completions"))
        request.httpMethod = "POST"
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "model": LocalInferenceModel.choices[1].id,
            "messages": [["role": "user", "content": "Reply with just pong."]],
            "max_tokens": 32, "temperature": 0,
        ] as [String: Any])
        let (data, response) = try await URLSession.shared.data(for: request)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200, String(decoding: data, as: UTF8.self))
        XCTAssertTrue(String(decoding: data, as: UTF8.self).contains("pong"))
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "model": LocalInferenceModel.choices[1].id,
            "messages": [["role": "user", "content": "Call echo with text pong."]],
            "tools": [["type": "function", "function": [
                "name": "echo", "description": "Repeat the text", "parameters": [
                    "type": "object", "properties": ["text": ["type": "string"]], "required": ["text"],
                ],
            ]]],
            "tool_choice": ["type": "function", "function": ["name": "echo"]],
            "max_tokens": 128, "temperature": 0, "stream": true,
        ] as [String: Any])
        let (stream, streamResponse) = try await URLSession.shared.data(for: request)
        XCTAssertEqual((streamResponse as? HTTPURLResponse)?.statusCode, 200, String(decoding: stream, as: UTF8.self))
        var arguments = ""
        var functionName = ""
        for line in String(decoding: stream, as: UTF8.self).split(separator: "\n") where line.hasPrefix("data:") {
            let payload = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
            if payload == "[DONE]" { continue }
            let chunk = try JSONSerialization.jsonObject(with: Data(payload.utf8)) as? [String: Any]
            let choices = chunk?["choices"] as? [[String: Any]]
            let delta = choices?.first?["delta"] as? [String: Any]
            let calls = delta?["tool_calls"] as? [[String: Any]]
            let function = calls?.first?["function"] as? [String: Any]
            arguments += function?["arguments"] as? String ?? ""
            functionName += function?["name"] as? String ?? ""
        }
        XCTAssertEqual(functionName, "echo")
        let toolArguments = try JSONSerialization.jsonObject(with: Data(arguments.utf8)) as? [String: String]
        XCTAssertEqual(toolArguments?["text"], "pong")
        let configured = try Data(contentsOf: config)
        controller.setup(model: LocalInferenceModel.choices[0]) { XCTFail("Cancelled setup must not apply the new model") }
        for _ in 0..<180 {
            if (controller.progress ?? 0) > 0 || !controller.busy { break }
            try await Task.sleep(for: .seconds(1))
        }
        XCTAssertNotNil(controller.progress, controller.message)
        let downloadedFraction = controller.progress ?? 0
        controller.cancel()
        for _ in 0..<30 {
            if !controller.busy { break }
            try await Task.sleep(for: .seconds(1))
        }
        XCTAssertFalse(controller.busy, controller.message)
        XCTAssertTrue(controller.message.contains("cancelled"), controller.message)
        XCTAssertEqual(try Data(contentsOf: config), configured)
        controller.setup(model: LocalInferenceModel.choices[0]) { XCTFail("Resume cancelled before configuration") }
        for _ in 0..<180 {
            if (controller.progress ?? 0) >= downloadedFraction && (controller.progress ?? 0) > 0 { break }
            if !controller.busy { break }
            try await Task.sleep(for: .seconds(1))
        }
        XCTAssertGreaterThanOrEqual(controller.progress ?? 0, downloadedFraction, controller.message)
        controller.cancel()
        for _ in 0..<30 {
            if !controller.busy { break }
            try await Task.sleep(for: .seconds(1))
        }
        XCTAssertFalse(controller.busy, controller.message)
        XCTAssertEqual(try Data(contentsOf: config), configured)
        await controller.stop()
        XCTAssertFalse(controller.serverRunning)
        await controller.start()
        XCTAssertTrue(controller.serverRunning, controller.message)
    }
}
