import AppKit
import WebKit

enum DashboardPopupPolicy {
    static func canOpen(
        _ target: URL, source: URL?, isMainFrame: Bool, sessionReady: Bool,
        dashboard: LocalDashboardConfiguration
    ) -> Bool {
        guard sessionReady, isMainFrame, let source, dashboard.contains(source) else { return false }
        return allowsNavigation(target, initialBlank: true, dashboard: dashboard)
    }

    static func allowsNavigation(
        _ target: URL, initialBlank: Bool, dashboard: LocalDashboardConfiguration
    ) -> Bool {
        if target.absoluteString == "about:blank" { return initialBlank }
        if dashboard.contains(target) { return true }
        guard target.scheme == "https", WebNavigationPolicy.isWebURL(target),
              let host = target.host else { return false }
        return !isLoopback(host)
    }

    private static func isLoopback(_ host: String) -> Bool {
        let lower = host.lowercased()
        return lower == "localhost" || lower.hasSuffix(".localhost")
            || lower == "::1" || lower == "[::1]" || lower.hasPrefix("127.")
    }
}

@MainActor
final class DashboardPopupWindow: NSObject, WKNavigationDelegate, WKUIDelegate, NSWindowDelegate {
    let id = UUID()
    let webView: WKWebView

    private let dashboard: LocalDashboardConfiguration
    private let window: NSWindow
    private let onClose: (UUID) -> Void
    private var initialBlank = true
    private var didClose = false

    init(configuration: WKWebViewConfiguration, dashboard: LocalDashboardConfiguration,
         onClose: @escaping (UUID) -> Void) {
        self.dashboard = dashboard
        self.onClose = onClose
        webView = WKWebView(frame: .zero, configuration: configuration)
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 500, height: 700),
            styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false
        )
        super.init()
        window.title = "Connect Account"
        window.minSize = NSSize(width: 380, height: 400)
        window.isReleasedWhenClosed = false
        window.contentView = webView
        window.delegate = self
        webView.navigationDelegate = self
        webView.uiDelegate = self
    }

    func show() {
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func close() {
        guard !didClose else { return }
        window.close()
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url,
              DashboardPopupPolicy.allowsNavigation(url, initialBlank: initialBlank, dashboard: dashboard) else {
            decisionHandler(.cancel)
            return
        }
        initialBlank = false
        decisionHandler(.allow)
    }

    func webViewDidClose(_ webView: WKWebView) { close() }

    func windowWillClose(_ notification: Notification) {
        guard !didClose else { return }
        didClose = true
        webView.stopLoading()
        webView.navigationDelegate = nil
        webView.uiDelegate = nil
        onClose(id)
    }
}
