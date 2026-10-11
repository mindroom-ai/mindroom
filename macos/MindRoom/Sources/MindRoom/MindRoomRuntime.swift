import Foundation

enum MindRoomRuntimeAction: Equatable {
    case installRuntime
    case updateRuntime
    case installService
    case startService
    case stopService
    case restartService
    case serviceStatus
    case checkSetup
    case initializeHostedConfig
    case initializeSelfHostedConfig
    case pairHosted
    case reconnectHosted
    case useLocalModel(String, String)
}

struct MindRoomCommandInvocation: Equatable {
    let executableURL: URL
    let arguments: [String]
    let environment: [String: String]
}

struct MindRoomRuntime {
    private static let bundledUVRelativePath = "Contents/Resources/bin/uv"
    // uv also accepts an existing Intel Python on Apple silicon, which cannot install the runtime's wheels.
    private static let runtimePython = "cpython-3.13-macos-aarch64-none"
    private static let desktopHelperRelativePath =
        "Contents/Helpers/MindRoom Desktop Helper.app/Contents/MacOS/MindRoom Desktop Helper"
    let homeURL: URL
    private let bundleURL: URL
    private let baseEnvironment: [String: String]
    private let appVersion: String?

    init(
        homeURL: URL = FileManager.default.homeDirectoryForCurrentUser,
        bundleURL: URL = Bundle.main.bundleURL,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        appVersion: String? = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
    ) {
        self.homeURL = homeURL
        self.bundleURL = bundleURL
        self.baseEnvironment = environment
        self.appVersion = appVersion
    }

    /// Release builds are stamped with their CalVer release tag, which is also the
    /// runtime's PyPI version, so the app installs exactly the runtime it was built
    /// with. Other builds, such as the build script's 0.1.0 development fallback,
    /// have no matching runtime release and install the latest one.
    var pinnedRuntimeVersion: String? {
        guard let appVersion, appVersion.range(of: #"^[0-9]{4}\.[0-9]+\.[0-9]+$"#, options: .regularExpression) != nil else {
            return nil
        }
        return appVersion
    }

    var bundledUVURL: URL {
        bundleURL.appendingPathComponent(Self.bundledUVRelativePath)
    }

    var bundledInferenceURL: URL {
        bundleURL.appendingPathComponent("Contents/Resources/llama.cpp")
    }

    var desktopHelperURL: URL {
        bundleURL.appendingPathComponent(Self.desktopHelperRelativePath)
    }

    var configDirectoryURL: URL {
        homeURL.appendingPathComponent(".mindroom", isDirectory: true)
    }

    var configPathURL: URL {
        configDirectoryURL.appendingPathComponent("config.yaml")
    }

    var envPathURL: URL {
        configDirectoryURL.appendingPathComponent(".env")
    }

    // The CLI stores these paths at service installation. Keep local-agent
    // setup aligned with that service without changing Computer access's home.
    private var localAgentServiceEnvironment: [String: String] {
        let plist = homeURL.appendingPathComponent("Library/LaunchAgents/chat.mindroom.local.plist")
        guard let data = try? Data(contentsOf: plist),
              let contents = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
              contents["Label"] as? String == "chat.mindroom.local",
              let environment = contents["EnvironmentVariables"] as? [String: String] else { return [:] }
        return environment.filter {
            ["MINDROOM_CONFIG_PATH", "MINDROOM_STORAGE_PATH"].contains($0.key) && $0.value.hasPrefix("/")
        }
    }

    var localAgentsConfigURL: URL {
        localAgentServiceEnvironment["MINDROOM_CONFIG_PATH"].map { URL(fileURLWithPath: $0) } ?? configPathURL
    }

    var logsDirectoryURL: URL {
        homeURL
            .appendingPathComponent("Library", isDirectory: true)
            .appendingPathComponent("Logs", isDirectory: true)
            .appendingPathComponent("mindroom", isDirectory: true)
    }

    func command(for action: MindRoomRuntimeAction) -> MindRoomCommandInvocation {
        switch action {
        case .installRuntime:
            return uvCommand(arguments: ["tool", "install", "--managed-python", "--python", Self.runtimePython, runtimeRequirement])
        case .updateRuntime:
            return uvCommand(arguments: ["tool", "install", "--managed-python", "--python", Self.runtimePython, "--force", runtimeRequirement])
        case .installService:
            return mindroomCommand(arguments: ["service", "install", "--no-confirm"])
        case .startService:
            return mindroomCommand(arguments: ["service", "start"])
        case .stopService:
            return mindroomCommand(arguments: ["service", "stop"])
        case .restartService:
            return mindroomCommand(arguments: ["service", "restart"])
        case .serviceStatus:
            return mindroomCommand(arguments: ["service", "status", "--logs", "0"])
        case .checkSetup:
            return mindroomCommand(arguments: ["doctor", "--config", localAgentsConfigURL.path])
        case .initializeHostedConfig:
            return mindroomCommand(arguments: ["config", "init", "--path", localAgentsConfigURL.path, "--matrix-server", "mindroom.chat", "--no-input"])
        case .initializeSelfHostedConfig:
            return mindroomCommand(arguments: ["config", "init", "--path", localAgentsConfigURL.path, "--matrix-server", "self-hosted", "--no-input"])
        case let .useLocalModel(model, apiKey):
            return mindroomCommand(arguments: ["config", "use-local-model", "--path", localAgentsConfigURL.path,
                                               "--model", model, "--base-url", LocalInferenceController.baseURL.absoluteString + "/v1",
                                               "--api-key", apiKey])
        case .pairHosted:
            return mindroomCommand(arguments: ["connect", "--graceful-cancel"])
        case .reconnectHosted:
            return mindroomCommand(arguments: ["connect", "--graceful-cancel", "--force"])
        }
    }

    func desktopHelperInvocation() -> MindRoomCommandInvocation {
        MindRoomCommandInvocation(
            executableURL: desktopHelperURL,
            arguments: [
                "--config", configPathURL.path,
            ],
            environment: commandEnvironment()
        )
    }

    func localSetupSnapshot() -> LocalAgentsSetupSnapshot {
        let executable = commandEnvironment()["PATH"]?.split(separator: ":")
            .map { URL(fileURLWithPath: String($0)).appendingPathComponent("mindroom").path }
            .first { FileManager.default.isExecutableFile(atPath: $0) }
        let serviceEnvironment = localAgentServiceEnvironment
        let config = serviceEnvironment["MINDROOM_CONFIG_PATH"].map { URL(fileURLWithPath: $0) } ?? configPathURL
        let configuration = try? config.resourceValues(forKeys: [.isRegularFileKey])
        let dates = [config, config.deletingLastPathComponent().appendingPathComponent(".env")].map {
            try? $0.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate
        }
        return LocalAgentsSetupSnapshot(
            runtimePath: executable, runtimeVersion: executable.flatMap(Self.installedRuntimeVersion(executablePath:)),
            requiredRuntimeVersion: pinnedRuntimeVersion, configurationExists: configuration?.isRegularFile == true,
            configurationDisplayPath: config.path.hasPrefix(homeURL.path + "/")
                ? "~" + config.path.dropFirst(homeURL.path.count) : config.path,
            configurationStamp: LocalAgentsConfigurationStamp(
                configurationURL: config, storagePath: serviceEnvironment["MINDROOM_STORAGE_PATH"], modificationDates: dates
            )
        )
    }

    private var runtimeRequirement: String {
        pinnedRuntimeVersion.map { "mindroom==\($0)" } ?? "mindroom"
    }

    /// Reads the installed version from the package metadata in the executable's
    /// Python environment, so status refreshes do not have to start the runtime.
    private static func installedRuntimeVersion(executablePath: String) -> String? {
        let fileManager = FileManager.default
        let lib = URL(fileURLWithPath: executablePath).resolvingSymlinksInPath()
            .deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("lib")
        let metadata = ((try? fileManager.contentsOfDirectory(atPath: lib.path)) ?? []).filter { $0.hasPrefix("python") }.flatMap { python -> [String] in
            let sitePackages = lib.appendingPathComponent(python).appendingPathComponent("site-packages")
            return ((try? fileManager.contentsOfDirectory(atPath: sitePackages.path)) ?? [])
                .filter { $0.hasPrefix("mindroom-") && $0.hasSuffix(".dist-info") }
        }
        // A prefix shared by several Python versions does not say which one the executable runs.
        guard metadata.count == 1, let entry = metadata.first else { return nil }
        return String(entry.dropFirst("mindroom-".count).dropLast(".dist-info".count))
    }

    private func uvCommand(arguments: [String]) -> MindRoomCommandInvocation {
        MindRoomCommandInvocation(
            executableURL: bundledUVURL,
            arguments: arguments,
            environment: commandEnvironment()
        )
    }

    private func mindroomCommand(arguments: [String]) -> MindRoomCommandInvocation {
        var environment = commandEnvironment()
        environment["MINDROOM_CONFIG_PATH"] = localAgentsConfigURL.path
        environment.merge(localAgentServiceEnvironment) { _, serviceValue in serviceValue }
        return MindRoomCommandInvocation(
            executableURL: URL(fileURLWithPath: "/usr/bin/env"),
            arguments: ["mindroom"] + arguments,
            environment: environment
        )
    }

    func commandEnvironment() -> [String: String] {
        var environment = baseEnvironment
        environment["PATH"] = commandPath(existingPATH: environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
        environment["UV_NO_PROGRESS"] = "1"
        environment["NO_COLOR"] = "1"
        environment["TERM"] = "dumb"
        return environment
    }

    func commandPath(existingPATH: String) -> String {
        let entries = [
            homeURL.appendingPathComponent(".local/bin", isDirectory: true).path,
            bundledUVURL.deletingLastPathComponent().path,
            "/opt/homebrew/bin",
            "/usr/local/bin",
            existingPATH,
        ]

        var seen = Set<String>()
        return entries
            .flatMap { $0.split(separator: ":").map(String.init) }
            .filter { !$0.isEmpty }
            .filter { seen.insert($0).inserted }
            .joined(separator: ":")
    }
}
