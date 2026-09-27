import AppKit
import WebKit
import XCTest
@testable import MindRoom

@MainActor
final class EmbeddedWebTabsTests: XCTestCase {
    func testChatWebsiteValidationAndSaving() throws {
        let suite = "MindRoomChatWebsiteTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let preferences = ChatWebsitePreferences(defaults: defaults)
        XCTAssertEqual(preferences.url.absoluteString, "https://chat.mindroom.chat")
        for value in ["https://example.org/chat", "http://127.0.0.1:8878", "http://[::1]:8878/"] {
            XCTAssertTrue(preferences.save(value), value)
            XCTAssertEqual(preferences.url.absoluteString, value)
        }
        for value in ["http://example.org", "https://user@example.org", "https://example.org/?key=x",
                      "https://example.org/#secret", "file:///etc/passwd", "http://127.0.0.2:8878"] {
            XCTAssertFalse(preferences.save(value), value)
            XCTAssertEqual(preferences.url.absoluteString, "http://[::1]:8878/")
        }
    }

    func testSeparateWebsiteDataStores() {
        let tabs = EmbeddedWebTabs(desktop: DesktopControlStore(), preferences: ChatWebsitePreferences())
        XCTAssertTrue(tabs.chat.configuration.websiteDataStore === WKWebsiteDataStore.default())
        XCTAssertFalse(tabs.dashboard.configuration.websiteDataStore.isPersistent)
        XCTAssertFalse(tabs.chat.configuration.websiteDataStore === tabs.dashboard.configuration.websiteDataStore)
    }

    func testDashboardConfigurationUsesPrivateRequestWithoutPublishingSecret() async throws {
        var requested: [String] = []
        let store = DesktopControlStore(request: { action, parameters, _ in
            requested.append(action)
            XCTAssertTrue(parameters.isEmpty)
            return ["url": "http://127.0.0.1:8877", "api_key": "test-secret"]
        })
        let config = try await store.dashboardConfiguration()
        XCTAssertEqual(requested, ["dashboard_configuration"])
        XCTAssertEqual(config.url.absoluteString, "http://127.0.0.1:8877")
        XCTAssertEqual(config.apiKey, "test-secret")
        XCTAssertFalse(store.diagnosticsText.contains("test-secret"))
        XCTAssertFalse((store.errorMessage ?? "").contains("test-secret"))
    }

    func testDashboardFailureIsBoundedAndRetryRequestsFreshConfiguration() async {
        var requests = 0
        let desktop = DesktopControlStore(request: { _, _, _ in
            requests += 1
            throw NSError(domain: "sensitive-token-in-error", code: 1)
        })
        let tabs = EmbeddedWebTabs(desktop: desktop, preferences: ChatWebsitePreferences())
        tabs.openDashboard()
        for _ in 0 ..< 100 where tabs.dashboardError == nil { await Task.yield() }
        XCTAssertEqual(requests, 1)
        XCTAssertEqual(tabs.dashboardError, LocalDashboardError.connectionFailed.localizedDescription)
        XCTAssertFalse(tabs.dashboardError?.contains("sensitive-token") ?? false)
        tabs.openDashboard(force: true)
        for _ in 0 ..< 100 where requests < 2 { await Task.yield() }
        XCTAssertEqual(requests, 2)
    }

    func testChatFailureOffersRetryState() {
        let tabs = EmbeddedWebTabs(desktop: DesktopControlStore(), preferences: ChatWebsitePreferences())
        tabs.webView(tabs.chat, didFail: nil, withError: NSError(domain: "private-network-error", code: 1))
        XCTAssertEqual(tabs.chatError, "Cannot load Chat. Check your connection or Chat website, then retry.")
        tabs.openChat(force: true)
        XCTAssertNil(tabs.chatError)
        XCTAssertTrue(tabs.chatLoading)
        tabs.chat.stopLoading()
    }

    func testDashboardNavigationRequiresExactOrigin() throws {
        let config = try LocalDashboardConfiguration(url: "http://127.0.0.1:8877", apiKey: nil)
        XCTAssertEqual(WebNavigationPolicy.dashboard(URL(string: "http://127.0.0.1:8877/agents")!,
                                                       clicked: false, configuration: config), .allow)
        XCTAssertEqual(WebNavigationPolicy.dashboard(URL(string: "https://example.org")!,
                                                       clicked: true, configuration: config), .openBrowser)
        XCTAssertEqual(WebNavigationPolicy.dashboard(URL(string: "http://127.0.0.1:8878")!,
                                                       clicked: false, configuration: config), .cancel)
    }

    func testChatLoginRedirectAndExternalClick() {
        let root = URL(string: "https://chat.mindroom.chat")!
        XCTAssertEqual(WebNavigationPolicy.chat(URL(string: "https://login.example.org/start")!,
                                                  clicked: false, root: root), .allow)
        XCTAssertEqual(WebNavigationPolicy.chat(URL(string: "https://example.org/help")!,
                                                  clicked: true, root: root), .openBrowser)
        XCTAssertEqual(WebNavigationPolicy.chat(URL(string: "file:///etc/passwd")!,
                                                  clicked: false, root: root), .cancel)
    }

    func testMatrixSSOStaysEmbeddedThroughProviderAndCallback() {
        let chat = URL(string: "https://chat.example.org/app")!
        var policy = ChatSSONavigation(root: chat)
        let start = URL(string: "https://matrix.example.net/_matrix/client/v3/login/sso/redirect/google?redirectUrl=https%3A%2F%2Fchat.example.org%2Flogin%2Fmatrix.example.net")!
        XCTAssertEqual(policy.decide(start, clicked: true), .allow)
        XCTAssertTrue(policy.isActive)
        XCTAssertEqual(policy.decide(URL(string: "https://accounts.example.net/oauth/choose")!, clicked: true), .allow)
        let callback = URL(string: "https://chat.example.org/login/matrix.example.net?loginToken=test")!
        XCTAssertEqual(policy.decide(callback, clicked: false), .allow)
        policy.finished(callback)
        XCTAssertFalse(policy.isActive)
        XCTAssertEqual(policy.decide(URL(string: "https://external.example.net/help")!, clicked: true), .openBrowser)
    }

    func testMatrixSSOEntryRequiresMatchingChatCallback() {
        let chat = URL(string: "https://custom-chat.example.org")!
        let wrong = [
            "https://matrix.example.net/_matrix/client/v3/login/sso/redirect/google?redirectUrl=https%3A%2F%2Fother.example.org%2Flogin",
            "https://matrix.example.net/_matrix/client/v3/login/sso/redirect/google?redirectUrl=https%3A%2F%2Fcustom-chat.example.org.evil.test%2Flogin",
            "https://matrix.example.net/_matrix/client/v3/login/sso/redirect/google?redirectUrl=https%3A%2F%2Fcustom-chat.example.org%2Flogin&redirectUrl=https%3A%2F%2Fevil.test",
            "https://matrix.example.net/not-matrix/login?redirectUrl=https%3A%2F%2Fcustom-chat.example.org%2Flogin",
            "http://matrix.example.net/_matrix/client/v3/login/sso/redirect/google?redirectUrl=https%3A%2F%2Fcustom-chat.example.org%2Flogin",
            "https://matrix.example.net/_matrix/client/v3/login/sso/redirect/google?redirectUrl=https%3A%2F%2Fuser%40custom-chat.example.org%2Flogin",
        ]
        for value in wrong {
            var policy = ChatSSONavigation(root: chat)
            XCTAssertEqual(policy.decide(URL(string: value)!, clicked: true), .openBrowser, value)
            XCTAssertFalse(policy.isActive)
        }
    }

    func testLoopbackChatCanUseLoopbackMatrixSSO() {
        let chat = URL(string: "http://127.0.0.1:8878")!
        var policy = ChatSSONavigation(root: chat)
        let start = URL(string: "http://127.0.0.1:8008/_matrix/client/v3/login/sso/redirect?redirectUrl=http%3A%2F%2F127.0.0.1%3A8878%2Flogin")!
        XCTAssertEqual(policy.decide(start, clicked: true), .allow)
        XCTAssertEqual(policy.decide(URL(string: "http://127.0.0.1:8008/_synapse/client/oidc/callback")!, clicked: false), .allow)
        XCTAssertEqual(policy.decide(URL(string: "http://evil.example.net")!, clicked: false), .cancel)
        XCTAssertEqual(policy.decide(URL(string: "about:blank")!, clicked: false), .cancel)
    }
}
