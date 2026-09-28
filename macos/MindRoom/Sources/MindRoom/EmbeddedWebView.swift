import AppKit
import SwiftUI
import WebKit

enum WebNavigationDecision: Equatable {
    case allow
    case cancel
    case openBrowser
}

enum WebNavigationPolicy {
    static func dashboard(_ url: URL, clicked: Bool, configuration: LocalDashboardConfiguration) -> WebNavigationDecision {
        if configuration.contains(url) { return .allow }
        return clicked && isWebURL(url) ? .openBrowser : .cancel
    }

    static func chat(_ url: URL, clicked: Bool, root: URL) -> WebNavigationDecision {
        guard isWebURL(url) else { return .cancel }
        let local = sameOrigin(url, root)
        if clicked && !local { return .openBrowser }
        if url.scheme == "https" { return .allow }
        return local && root.scheme == "http" ? .allow : .cancel
    }

    static func isWebURL(_ url: URL) -> Bool {
        (url.scheme == "https" || url.scheme == "http") && url.host != nil
            && url.user == nil && url.password == nil
    }

    static func sameOrigin(_ first: URL, _ second: URL) -> Bool {
        first.scheme == second.scheme && first.host == second.host
            && (first.port ?? defaultPort(first.scheme)) == (second.port ?? defaultPort(second.scheme))
    }

    private static func defaultPort(_ scheme: String?) -> Int? {
        switch scheme {
        case "https": return 443
        case "http": return 80
        default: return nil
        }
    }
}

struct ChatSSONavigation {
    let root: URL
    private(set) var isActive = false
    private var localHomeserver: URL?

    init(root: URL) { self.root = root }

    mutating func decide(_ url: URL, clicked: Bool) -> WebNavigationDecision {
        guard WebNavigationPolicy.isWebURL(url) else { return .cancel }
        if clicked && Self.isStart(url, returningTo: root) {
            isActive = true
            localHomeserver = url.scheme == "http" ? url : nil
            return .allow
        }
        if WebNavigationPolicy.sameOrigin(url, root) { return .allow }
        if isActive {
            if url.scheme == "https" { return .allow }
            if let localHomeserver, WebNavigationPolicy.sameOrigin(url, localHomeserver) { return .allow }
            return .cancel
        }
        return WebNavigationPolicy.chat(url, clicked: clicked, root: root)
    }

    mutating func finished(_ url: URL?) {
        guard let url, WebNavigationPolicy.sameOrigin(url, root) else { return }
        isActive = false
        localHomeserver = nil
    }

    static func isStart(_ url: URL, returningTo root: URL) -> Bool {
        guard WebNavigationPolicy.isWebURL(url),
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.fragment == nil,
              components.percentEncodedPath.range(
                of: #"\A/_matrix/client/(?:v3|r0)/login/sso/redirect(?:/[^/]+)?\z"#,
                options: .regularExpression
              ) != nil else { return false }
        if url.scheme == "http" {
            guard root.scheme == "http", isLoopback(root.host), isLoopback(url.host) else { return false }
        }
        let callbacks = components.queryItems?.filter { $0.name == "redirectUrl" } ?? []
        guard callbacks.count == 1,
              let callback = callbacks[0].value.flatMap(URL.init(string:)),
              WebNavigationPolicy.isWebURL(callback) else { return false }
        return WebNavigationPolicy.sameOrigin(callback, root)
    }

    private static func isLoopback(_ host: String?) -> Bool {
        guard let host else { return false }
        return ["localhost", "127.0.0.1", "::1", "[::1]"].contains(host.lowercased())
    }
}

@MainActor
final class EmbeddedWebTabs: NSObject, ObservableObject, WKNavigationDelegate, WKUIDelegate {
    let chat: WKWebView
    @Published private(set) var dashboard: WKWebView
    let preferences: ChatWebsitePreferences

    @Published private(set) var chatError: String?
    @Published private(set) var dashboardError: String?
    @Published private(set) var chatLoading = false
    @Published private(set) var dashboardLoading = false

    private let desktop: DesktopControlStore
    private var dashboardConfiguration: LocalDashboardConfiguration?
    private var chatSSO: ChatSSONavigation
    private var chatLoadedURL: URL?
    private var dashboardLoaded = false
    private var dashboardAttempt = 0
    private var dashboardPopups: [UUID: DashboardPopupWindow] = [:]

    init(desktop: DesktopControlStore, preferences: ChatWebsitePreferences) {
        self.desktop = desktop
        self.preferences = preferences
        chatSSO = ChatSSONavigation(root: preferences.url)
        let chatConfig = WKWebViewConfiguration()
        chatConfig.websiteDataStore = .default()
        chat = WKWebView(frame: .zero, configuration: chatConfig)
        let dashboardConfig = WKWebViewConfiguration()
        dashboardConfig.websiteDataStore = .nonPersistent()
        dashboard = WKWebView(frame: .zero, configuration: dashboardConfig)
        super.init()
        chat.navigationDelegate = self
        chat.uiDelegate = self
        dashboard.navigationDelegate = self
        dashboard.uiDelegate = self
    }

    func openChat(url: URL? = nil, force: Bool = false) {
        let url = url ?? preferences.url
        guard force || chatLoadedURL != url else { return }
        chatSSO = ChatSSONavigation(root: url)
        chatLoadedURL = url
        chatError = nil
        chatLoading = true
        chat.load(URLRequest(url: url))
    }

    func canStartChatSSOPopup(_ url: URL, from source: URL) -> Bool {
        WebNavigationPolicy.sameOrigin(source, chatSSO.root)
            && ChatSSONavigation.isStart(url, returningTo: chatSSO.root)
    }

    func openDashboard(force: Bool = false) {
        guard force || !dashboardLoaded else { return }
        dashboardAttempt += 1
        let attempt = dashboardAttempt
        dashboardError = nil
        dashboardLoading = true
        replaceDashboardWebView()
        let webView = dashboard
        Task {
            do {
                let configuration = try await desktop.dashboardConfiguration()
                let cookie = try await configuration.login()
                guard attempt == dashboardAttempt, webView === dashboard else { return }
                dashboardConfiguration = configuration
                if let cookie {
                    await webView.configuration.websiteDataStore.httpCookieStore.setCookie(cookie)
                }
                guard attempt == dashboardAttempt, webView === dashboard else { return }
                dashboardLoaded = true
                webView.load(URLRequest(url: configuration.url))
            } catch let error as LocalDashboardError {
                guard attempt == dashboardAttempt else { return }
                dashboardLoading = false
                dashboardError = error.localizedDescription
            } catch {
                guard attempt == dashboardAttempt else { return }
                dashboardLoading = false
                dashboardError = LocalDashboardError.connectionFailed.localizedDescription
            }
        }
    }

    func serviceStopped() {
        dashboardAttempt += 1
        dashboardLoading = false
        replaceDashboardWebView()
    }

    private func replaceDashboardWebView() {
        for popup in Array(dashboardPopups.values) { popup.close() }
        dashboardPopups.removeAll()
        dashboard.stopLoading()
        dashboard.navigationDelegate = nil
        dashboard.uiDelegate = nil
        dashboardConfiguration = nil
        dashboardLoaded = false
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let replacement = WKWebView(frame: .zero, configuration: configuration)
        replacement.navigationDelegate = self
        replacement.uiDelegate = self
        dashboard = replacement
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        let clicked = action.navigationType == .linkActivated
        let decision: WebNavigationDecision
        if webView === dashboard {
            guard let dashboardConfiguration else { decisionHandler(.cancel); return }
            if action.targetFrame == nil && !clicked {
                let allowed = DashboardPopupPolicy.canOpen(
                    url, source: action.sourceFrame.request.url,
                    isMainFrame: action.sourceFrame.isMainFrame,
                    sessionReady: dashboardLoaded && !dashboardLoading && dashboardError == nil,
                    dashboard: dashboardConfiguration
                )
                decisionHandler(allowed ? .allow : .cancel)
                return
            }
            decision = WebNavigationPolicy.dashboard(url, clicked: clicked, configuration: dashboardConfiguration)
        } else {
            decision = chatSSO.decide(url, clicked: clicked)
        }
        if action.targetFrame == nil && decision != .cancel {
            // Let the UI delegate route new-window requests exactly once.
            decisionHandler(.allow)
            return
        }
        if decision == .openBrowser { NSWorkspace.shared.open(url) }
        decisionHandler(decision == .allow ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard let url = action.request.url else { return nil }
        let clicked = action.navigationType == .linkActivated
        if webView === dashboard {
            guard let dashboardConfiguration else { return nil }
            if clicked {
                switch WebNavigationPolicy.dashboard(url, clicked: true, configuration: dashboardConfiguration) {
                case .allow: webView.load(URLRequest(url: url))
                case .openBrowser: NSWorkspace.shared.open(url)
                case .cancel: break
                }
                return nil
            }
            guard DashboardPopupPolicy.canOpen(
                url, source: action.sourceFrame.request.url,
                isMainFrame: action.sourceFrame.isMainFrame,
                sessionReady: dashboardLoaded && !dashboardLoading && dashboardError == nil,
                dashboard: dashboardConfiguration
            ) else { return nil }
            let popup = DashboardPopupWindow(configuration: configuration, dashboard: dashboardConfiguration) {
                [weak self] id in self?.dashboardPopups.removeValue(forKey: id)
            }
            dashboardPopups[popup.id] = popup
            popup.show()
            return popup.webView
        }
        guard WebNavigationPolicy.isWebURL(url) else { return nil }
        if !clicked {
            // Some SSO buttons use window.open. Accept only a Matrix SSO start from Chat itself.
            guard webView === chat, action.targetFrame == nil,
                  let source = action.sourceFrame.request.url,
                  canStartChatSSOPopup(url, from: source) else { return nil }
        }
        switch chatSSO.decide(url, clicked: true) {
        case .allow: webView.load(URLRequest(url: url))
        case .openBrowser: NSWorkspace.shared.open(url)
        case .cancel: break
        }
        return nil
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if webView === chat {
            chatLoading = false
            chatError = nil
            chatSSO.finished(webView.url)
        }
        else { dashboardLoading = false; dashboardError = nil }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { navigationFailed(webView) }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { navigationFailed(webView) }
    }

    private func navigationFailed(_ webView: WKWebView) {
        if webView === chat {
            chatLoading = false
            chatError = "Cannot load Chat. Check your connection or Chat website, then retry."
            chatSSO = ChatSSONavigation(root: chatSSO.root)
        } else {
            dashboardLoading = false
            dashboardError = LocalDashboardError.connectionFailed.localizedDescription
            dashboardLoaded = false
        }
    }
}

struct EmbeddedWebView: NSViewRepresentable {
    let webView: WKWebView

    func makeNSView(context: Context) -> WKWebView { webView }
    func updateNSView(_ nsView: WKWebView, context: Context) {}
}
