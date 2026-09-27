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
        let sameOrigin = url.scheme == root.scheme && url.host == root.host && url.port == root.port
        if clicked && !sameOrigin { return .openBrowser }
        if url.scheme == "https" { return .allow }
        return sameOrigin && root.scheme == "http" ? .allow : .cancel
    }

    static func isWebURL(_ url: URL) -> Bool {
        (url.scheme == "https" || url.scheme == "http") && url.host != nil
            && url.user == nil && url.password == nil
    }
}

@MainActor
final class EmbeddedWebTabs: NSObject, ObservableObject, WKNavigationDelegate, WKUIDelegate {
    let chat: WKWebView
    let dashboard: WKWebView
    let preferences: ChatWebsitePreferences

    @Published private(set) var chatError: String?
    @Published private(set) var dashboardError: String?
    @Published private(set) var chatLoading = false
    @Published private(set) var dashboardLoading = false

    private let desktop: DesktopControlStore
    private var dashboardConfiguration: LocalDashboardConfiguration?
    private var chatLoadedURL: URL?
    private var dashboardLoaded = false
    private var dashboardAttempt = 0

    init(desktop: DesktopControlStore, preferences: ChatWebsitePreferences) {
        self.desktop = desktop
        self.preferences = preferences
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

    func openChat(force: Bool = false) {
        let url = preferences.url
        guard force || chatLoadedURL != url else { return }
        chatLoadedURL = url
        chatError = nil
        chatLoading = true
        chat.load(URLRequest(url: url))
    }

    func openDashboard(force: Bool = false) {
        guard force || !dashboardLoaded else { return }
        dashboardAttempt += 1
        let attempt = dashboardAttempt
        dashboardError = nil
        dashboardLoading = true
        Task {
            do {
                let configuration = try await desktop.dashboardConfiguration()
                let cookie = try await configuration.login()
                guard attempt == dashboardAttempt else { return }
                dashboardConfiguration = configuration
                if let cookie {
                    await dashboard.configuration.websiteDataStore.httpCookieStore.setCookie(cookie)
                }
                guard attempt == dashboardAttempt else { return }
                dashboardLoaded = true
                dashboard.load(URLRequest(url: configuration.url))
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
        dashboardLoaded = false
        dashboardConfiguration = nil
        dashboardLoading = false
        dashboard.stopLoading()
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        let clicked = action.navigationType == .linkActivated
        let decision: WebNavigationDecision
        if webView === dashboard {
            guard let dashboardConfiguration else { decisionHandler(.cancel); return }
            decision = WebNavigationPolicy.dashboard(url, clicked: clicked, configuration: dashboardConfiguration)
        } else {
            decision = WebNavigationPolicy.chat(url, clicked: clicked, root: preferences.url)
        }
        if decision == .openBrowser { NSWorkspace.shared.open(url) }
        decisionHandler(decision == .allow ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard action.navigationType == .linkActivated,
              let url = action.request.url, WebNavigationPolicy.isWebURL(url) else { return nil }
        if webView === dashboard {
            guard let dashboardConfiguration else { return nil }
            switch WebNavigationPolicy.dashboard(url, clicked: true, configuration: dashboardConfiguration) {
            case .allow: webView.load(URLRequest(url: url))
            case .openBrowser: NSWorkspace.shared.open(url)
            case .cancel: break
            }
        } else {
            switch WebNavigationPolicy.chat(url, clicked: true, root: preferences.url) {
            case .allow: webView.load(URLRequest(url: url))
            case .openBrowser: NSWorkspace.shared.open(url)
            case .cancel: break
            }
        }
        return nil
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if webView === chat { chatLoading = false; chatError = nil }
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
