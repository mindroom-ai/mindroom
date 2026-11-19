import Foundation

/// Finds the MindRoom Chat disk image to install in GitHub's list of releases.
enum ChatAppRelease {
    static let listURL = URL(string: "https://api.github.com/repos/mindroom-ai/mindroom-chat/releases?per_page=10")!
    /// The image's name on a release, then the name older releases gave it.
    private static let imageNames = ["MindRoom-Chat.dmg", "MindRoom.dmg"]
    private static let downloads = "https://github.com/mindroom-ai/mindroom-chat/releases/download/"

    private struct Release: Decodable {
        struct Asset: Decodable {
            let name: String
            let state: String
            let browser_download_url: String
        }

        let draft: Bool
        let prerelease: Bool
        let assets: [Asset]
    }

    /// The image of the newest published release that has one, from GitHub's list (newest first).
    /// The newest release goes without it for some minutes, until the Mac build has attached it,
    /// and a failed upload leaves a `starter` asset with nothing to download.
    static func downloadURL(fromReleaseList data: Data) -> URL? {
        guard let releases = try? JSONDecoder().decode([Release].self, from: data) else { return nil }
        for release in releases where !release.draft && !release.prerelease {
            for name in imageNames {
                guard let asset = release.assets.first(where: { $0.name == name && $0.state == "uploaded" }) else { continue }
                return asset.browser_download_url.hasPrefix(downloads) ? URL(string: asset.browser_download_url) : nil
            }
        }
        return nil
    }
}
