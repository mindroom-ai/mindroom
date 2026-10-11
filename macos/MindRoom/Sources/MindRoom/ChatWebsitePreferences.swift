import Foundation

@MainActor
final class ChatWebsitePreferences: ObservableObject {
    static let defaultURL = URL(string: "https://chat.mindroom.chat")!
    private static let key = "chatWebsiteURL"

    @Published private(set) var url: URL
    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        self.url = defaults.string(forKey: Self.key).flatMap(Self.validatedURL) ?? Self.defaultURL
    }

    @discardableResult
    func save(_ text: String) -> Bool {
        guard let validated = Self.validatedURL(text) else { return false }
        defaults.set(validated.absoluteString, forKey: Self.key)
        url = validated
        return true
    }

    static func validatedURL(_ text: String) -> URL? {
        guard !text.isEmpty,
              text.unicodeScalars.allSatisfy({ $0.value >= 0x21 && $0.value <= 0x7e }),
              let parts = URLComponents(string: text),
              let scheme = parts.scheme?.lowercased(),
              let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil,
              parts.query == nil, parts.fragment == nil,
              let url = parts.url,
              url.absoluteString == text else { return nil }
        if scheme == "https" { return url }
        guard scheme == "http", ["localhost", "127.0.0.1", "::1", "[::1]"].contains(host.lowercased()) else {
            return nil
        }
        return url
    }
}
