import Foundation
import WebKit
import XCTest
@testable import MindRoom

@MainActor
final class DashboardPopupTests: XCTestCase {
    func testPopupUsesWebKitProvidedStore() throws {
        let dashboard = try LocalDashboardConfiguration(url: "http://127.0.0.1:8877", apiKey: nil)
        let supplied = WKWebViewConfiguration()
        supplied.websiteDataStore = .nonPersistent()
        var closed: UUID?
        let popup = DashboardPopupWindow(configuration: supplied, dashboard: dashboard, onClose: { closed = $0 })
        XCTAssertTrue(popup.webView.configuration.websiteDataStore === supplied.websiteDataStore)
        popup.close()
        XCTAssertEqual(closed, popup.id)
    }
    func testOnlyAuthenticatedDashboardMainFrameCanLaunchPopup() throws {
        let dashboard = try LocalDashboardConfiguration(url: "http://127.0.0.1:8877", apiKey: nil)
        let source = URL(string: "http://127.0.0.1:8877/agents")!
        let blank = URL(string: "about:blank")!
        let auth = URL(string: "https://accounts.example.org/oauth")!
        XCTAssertTrue(DashboardPopupPolicy.canOpen(blank, source: source, isMainFrame: true,
                                                   sessionReady: true, dashboard: dashboard))
        XCTAssertTrue(DashboardPopupPolicy.canOpen(auth, source: source, isMainFrame: true,
                                                   sessionReady: true, dashboard: dashboard))
        XCTAssertFalse(DashboardPopupPolicy.canOpen(blank, source: source, isMainFrame: false,
                                                    sessionReady: true, dashboard: dashboard))
        XCTAssertFalse(DashboardPopupPolicy.canOpen(blank, source: source, isMainFrame: true,
                                                    sessionReady: false, dashboard: dashboard))
        XCTAssertFalse(DashboardPopupPolicy.canOpen(blank, source: URL(string: "http://127.0.0.1:8878")!,
                                                    isMainFrame: true, sessionReady: true, dashboard: dashboard))
    }

    func testPopupAllowsAuthAndCallbackButNoOtherLocalOrigin() throws {
        let dashboard = try LocalDashboardConfiguration(url: "http://127.0.0.1:8877", apiKey: nil)
        for value in ["https://accounts.example.org/oauth", "https://login.example.org/callback",
                      "http://127.0.0.1:8877/api/oauth/callback"] {
            XCTAssertTrue(DashboardPopupPolicy.allowsNavigation(URL(string: value)!,
                                                                  initialBlank: false, dashboard: dashboard), value)
        }
        XCTAssertTrue(DashboardPopupPolicy.allowsNavigation(URL(string: "about:blank")!,
                                                         initialBlank: true, dashboard: dashboard))
        for value in ["about:blank", "http://127.0.0.1:8878/steal", "https://127.0.0.1:8878/steal",
                      "http://localhost:8877/steal", "http://evil.example.org", "file:///etc/passwd"] {
            XCTAssertFalse(DashboardPopupPolicy.allowsNavigation(URL(string: value)!,
                                                                   initialBlank: false, dashboard: dashboard), value)
        }
    }
}
