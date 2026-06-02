import Foundation
import Sparkle

enum AppUpdaterError: LocalizedError {
    case updatesNotConfigured

    var errorDescription: String? {
        switch self {
        case .updatesNotConfigured:
            return "App updates are not configured for this build"
        }
    }
}

@MainActor
final class AppUpdater: ObservableObject {
    static let shared = AppUpdater()

    private let updaterController: SPUStandardUpdaterController?

    var canCheckForUpdates: Bool {
        updaterController != nil
    }

    private init(configuration: SparkleConfiguration = AppMetadata.sparkleConfiguration) {
        guard configuration.isConfigured else {
            updaterController = nil
            return
        }
        updaterController = SPUStandardUpdaterController(
            startingUpdater: true,
            updaterDelegate: nil,
            userDriverDelegate: nil
        )
    }

    func checkForUpdates() throws {
        guard let updaterController else {
            throw AppUpdaterError.updatesNotConfigured
        }
        updaterController.checkForUpdates(nil)
    }
}
