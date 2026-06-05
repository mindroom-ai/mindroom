import Foundation

enum MindRoomRuntimeAction: Equatable {
    case installRuntime
    case updateRuntime
    case installService
    case startService
    case stopService
    case restartService
    case serviceStatus
    case initializeHostedConfig
    case initializeSelfHostedConfig
    case localStackSetup
    case pairHosted(pairCode: String)
}

struct MindRoomCommandInvocation: Equatable {
    let executableURL: URL
    let arguments: [String]
    let environment: [String: String]
}

struct MindRoomRuntime {
    private static let bundledUVRelativePath = "Contents/Resources/bin/uv"
    private let homeURL: URL
    private let bundleURL: URL
    private let baseEnvironment: [String: String]

    init(
        homeURL: URL = FileManager.default.homeDirectoryForCurrentUser,
        bundleURL: URL = Bundle.main.bundleURL,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        self.homeURL = homeURL
        self.bundleURL = bundleURL
        self.baseEnvironment = environment
    }

    var bundledUVURL: URL {
        bundleURL.appendingPathComponent(Self.bundledUVRelativePath)
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

    var logsDirectoryURL: URL {
        homeURL
            .appendingPathComponent("Library", isDirectory: true)
            .appendingPathComponent("Logs", isDirectory: true)
            .appendingPathComponent("mindroom", isDirectory: true)
    }

    func command(for action: MindRoomRuntimeAction) -> MindRoomCommandInvocation {
        switch action {
        case .installRuntime:
            return uvCommand(arguments: ["tool", "install", "--managed-python", "--python", "3.13", "mindroom"])
        case .updateRuntime:
            return uvCommand(arguments: ["tool", "install", "--managed-python", "--python", "3.13", "--force", "mindroom"])
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
        case .initializeHostedConfig:
            return mindroomCommand(arguments: ["config", "init", "--matrix-server", "mindroom.chat"])
        case .initializeSelfHostedConfig:
            return mindroomCommand(arguments: ["config", "init", "--matrix-server", "self-hosted"])
        case .localStackSetup:
            return mindroomCommand(arguments: ["local-stack-setup"])
        case let .pairHosted(pairCode):
            return mindroomCommand(arguments: ["connect", "--pair-code", pairCode])
        }
    }

    private func uvCommand(arguments: [String]) -> MindRoomCommandInvocation {
        MindRoomCommandInvocation(
            executableURL: bundledUVURL,
            arguments: arguments,
            environment: commandEnvironment()
        )
    }

    private func mindroomCommand(arguments: [String]) -> MindRoomCommandInvocation {
        MindRoomCommandInvocation(
            executableURL: URL(fileURLWithPath: "/usr/bin/env"),
            arguments: ["mindroom"] + arguments,
            environment: commandEnvironment()
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
