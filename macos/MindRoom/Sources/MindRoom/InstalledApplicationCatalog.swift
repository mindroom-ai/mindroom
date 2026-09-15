import AppKit
import Foundation

struct InstalledDesktopApplication: Identifiable, Hashable {
    let id: String
    let name: String
    let running: Bool
}

@MainActor
enum InstalledApplicationCatalog {
    static func applications() -> [InstalledDesktopApplication] {
        let runningIDs = Set(NSWorkspace.shared.runningApplications.compactMap(\.bundleIdentifier))
        let roots = [
            URL(fileURLWithPath: "/Applications", isDirectory: true),
            URL(fileURLWithPath: "/System/Applications", isDirectory: true),
            FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications", isDirectory: true),
        ]
        var found: [String: InstalledDesktopApplication] = [:]
        for root in roots {
            guard let enumerator = FileManager.default.enumerator(
                at: root,
                includingPropertiesForKeys: [.isApplicationKey],
                options: [.skipsHiddenFiles, .skipsPackageDescendants]
            ) else { continue }
            for case let url as URL in enumerator where url.pathExtension == "app" {
                guard let bundle = Bundle(url: url), let identifier = bundle.bundleIdentifier else { continue }
                let name = (bundle.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String)
                    ?? (bundle.object(forInfoDictionaryKey: "CFBundleName") as? String)
                    ?? url.deletingPathExtension().lastPathComponent
                found[identifier] = InstalledDesktopApplication(
                    id: identifier,
                    name: name,
                    running: runningIDs.contains(identifier)
                )
            }
        }
        found["primary-screen"] = InstalledDesktopApplication(
            id: "primary-screen",
            name: "Primary Screen (advanced coordinate fallback)",
            running: true
        )
        return found.values.sorted {
            $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending
        }
    }
}
