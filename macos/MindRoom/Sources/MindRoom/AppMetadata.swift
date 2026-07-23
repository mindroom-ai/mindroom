import Foundation

struct SparkleConfiguration {
    let feedURL: String?
    let publicEdKey: String?

    var isConfigured: Bool {
        guard let feedURL, !feedURL.isEmpty else { return false }
        guard let publicEdKey, !publicEdKey.isEmpty else { return false }
        return true
    }
}

enum AppMetadata {
    static var sparkleConfiguration: SparkleConfiguration {
        SparkleConfiguration(
            feedURL: Bundle.main.object(forInfoDictionaryKey: "SUFeedURL") as? String,
            publicEdKey: Bundle.main.object(forInfoDictionaryKey: "SUPublicEDKey") as? String
        )
    }
}
