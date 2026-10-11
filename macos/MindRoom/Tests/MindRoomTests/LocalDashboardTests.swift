import Foundation
import XCTest
@testable import MindRoom

final class LocalDashboardTests: XCTestCase {
    func testCredentialsOnlyGoToLocalSessionEndpointInPostBody() throws {
        let config = try LocalDashboardConfiguration(url: "http://127.0.0.1:8765", apiKey: "test-secret")
        let request = try XCTUnwrap(config.loginRequest)
        XCTAssertEqual(request.url?.absoluteString, "http://127.0.0.1:8765/api/auth/session")
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
        let body = try JSONSerialization.jsonObject(with: XCTUnwrap(request.httpBody)) as? [String: String]
        XCTAssertEqual(body, ["api_key": "test-secret"])
        XCTAssertNil(try LocalDashboardConfiguration(url: "http://[::1]:8765/", apiKey: nil).loginRequest)
    }

    func testRejectsDestinationsOutsideLiteralLoopback() {
        for value in ["https://example.com", "http://localhost", "http://127.0.0.1.evil.test",
                      "http://user:secret@127.0.0.1", "http://127.0.0.1?key=secret", "http://127.0.0.1/#secret",
                      "file:///etc/passwd", "http://127.0.0.1/other", "http://127.0.0.1:0"] {
            XCTAssertThrowsError(try LocalDashboardConfiguration(url: value, apiKey: "test-secret"), value)
        }
    }

    func testEmbeddedNavigationStaysOnExactOrigin() throws {
        let config = try LocalDashboardConfiguration(url: "http://127.0.0.1:8765", apiKey: nil)
        XCTAssertTrue(config.contains(URL(string: "http://127.0.0.1:8765/agents?name=one")!))
        for value in ["http://127.0.0.1:9999", "https://127.0.0.1:8765", "https://example.com",
                      "http://user@127.0.0.1:8765", "file:///etc/passwd"] {
            XCTAssertFalse(config.contains(URL(string: value)!), value)
        }
    }

    func testAcceptsOnlyExpectedHttpOnlyHostCookie() throws {
        let config = try LocalDashboardConfiguration(url: "http://127.0.0.1:8765", apiKey: "test-secret")
        let endpoint = URL(string: "http://127.0.0.1:8765/api/auth/session")!
        let accepted = HTTPURLResponse(
            url: endpoint, statusCode: 200, httpVersion: nil,
            headerFields: ["Set-Cookie": "mindroom_api_key=test-secret; HttpOnly; Path=/; SameSite=lax"]
        )!
        XCTAssertEqual(try config.sessionCookie(from: accepted).name, "mindroom_api_key")

        for (status, header) in [
            (302, "mindroom_api_key=test-secret; HttpOnly; Path=/"),
            (200, "mindroom_api_key=test-secret; Path=/"),
            (200, "other=test-secret; HttpOnly; Path=/"),
            (200, "mindroom_api_key=test-secret; HttpOnly; Path=/; Domain=127.0.0.1"),
            (200, "mindroom_api_key=test-secret; HttpOnly; Path=/; DOMAIN = 127.0.0.1"),
            (200, "mindroom_api_key=wrong; HttpOnly; Path=/"),
            (200, "mindroom_api_key=test-secret; HttpOnly; Path=/other"),
        ] {
            let response = HTTPURLResponse(
                url: endpoint, statusCode: status, httpVersion: nil,
                headerFields: ["Set-Cookie": header]
            )!
            XCTAssertThrowsError(try config.sessionCookie(from: response), header)
        }
        let foreignEndpoint = URL(string: "http://127.0.0.1:8766/api/auth/session")!
        let foreignResponse = HTTPURLResponse(
            url: foreignEndpoint, statusCode: 200, httpVersion: nil,
            headerFields: ["Set-Cookie": "mindroom_api_key=test-secret; HttpOnly; Path=/"]
        )!
        XCTAssertThrowsError(try config.sessionCookie(from: foreignResponse))
    }
}
