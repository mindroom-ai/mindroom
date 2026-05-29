import Foundation
import Sparkle

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

    func checkForUpdates() {
        guard let updaterController else {
            MindRoomCommandRunner.shared.lastOutputForDisplay = "App updates are not configured for this build"
            return
        }
        updaterController.checkForUpdates(nil)
    }
}
