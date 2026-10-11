import Foundation

enum LocalDashboardError: LocalizedError {
    case invalidConfiguration
    case connectionFailed
    case authenticationFailed

    var errorDescription: String? {
        switch self {
        case .invalidConfiguration:
            return "Local dashboard address is invalid. Check MINDROOM_URL and retry."
        case .connectionFailed:
            return "Cannot connect to the local dashboard. Start the local service and retry."
        case .authenticationFailed:
            return "Local dashboard sign-in failed. Check MINDROOM_API_KEY and retry."
        }
    }
}

struct LocalDashboardConfiguration {
    let url: URL
    let apiKey: String?

    init(url rawURL: String, apiKey: String?) throws {
        // Validate text before Foundation can normalize userinfo, escapes, or control characters.
        guard rawURL.unicodeScalars.allSatisfy({ $0.value >= 0x21 && $0.value <= 0x7e }),
              rawURL.range(
                of: #"\Ahttp://(?:localhost|127\.0\.0\.1|\[::1\]):[0-9]{1,5}/?\z"#,
                options: .regularExpression
              ) != nil,
              let parsed = URLComponents(string: rawURL),
              let port = parsed.port, (1...65535).contains(port),
              let host = parsed.host else {
            throw LocalDashboardError.invalidConfiguration
        }
        let normalizedHost = host == "localhost" ? "127.0.0.1" : host
        guard let canonicalURL = URL(string: "http://\(normalizedHost == "::1" ? "[::1]" : normalizedHost):\(port)") else {
            throw LocalDashboardError.invalidConfiguration
        }
        self.url = canonicalURL
        self.apiKey = apiKey
    }

    var loginRequest: URLRequest? {
        guard let apiKey, !apiKey.isEmpty,
              let endpoint = URL(string: "/api/auth/session", relativeTo: url)?.absoluteURL,
              let body = try? JSONSerialization.data(withJSONObject: ["api_key": apiKey]) else {
            return nil
        }
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpShouldHandleCookies = false
        return request
    }

    func contains(_ candidate: URL) -> Bool {
        candidate.scheme == "http" && candidate.host == url.host && candidate.port == url.port
            && candidate.user == nil && candidate.password == nil
    }

    func sessionCookie(from response: HTTPURLResponse) throws -> HTTPCookie {
        guard let apiKey, response.statusCode == 200,
              let endpoint = loginRequest?.url, response.url == endpoint,
              let rawHeader = response.value(forHTTPHeaderField: "Set-Cookie"),
              rawHeader.unicodeScalars.allSatisfy({ $0.value >= 0x20 && $0.value != 0x7f }),
              !rawHeader.split(separator: ";").dropFirst().contains(where: {
                  $0.split(separator: "=", maxSplits: 1).first?
                    .trimmingCharacters(in: .whitespaces).lowercased() == "domain"
              }) else {
            throw LocalDashboardError.authenticationFailed
        }
        let cookies = HTTPCookie.cookies(
            withResponseHeaderFields: ["Set-Cookie": rawHeader], for: endpoint
        )
        guard cookies.count == 1, let cookie = cookies.first,
              cookie.name == "mindroom_api_key", cookie.value == apiKey,
              cookie.path == "/", cookie.domain == endpoint.host,
              cookie.isHTTPOnly else {
            throw LocalDashboardError.authenticationFailed
        }
        return cookie
    }

    func login() async throws -> HTTPCookie? {
        guard let request = loginRequest else { return nil }
        let settings = URLSessionConfiguration.ephemeral
        settings.httpCookieStorage = nil
        settings.httpShouldSetCookies = false
        settings.urlCache = nil
        settings.urlCredentialStorage = nil
        let session = URLSession(configuration: settings)
        defer { session.invalidateAndCancel() }
        do {
            let (_, response) = try await session.data(for: request, delegate: NoDashboardRedirects())
            guard let http = response as? HTTPURLResponse else {
                throw LocalDashboardError.authenticationFailed
            }
            return try sessionCookie(from: http)
        } catch let error as LocalDashboardError {
            throw error
        } catch {
            // URLSession failures can contain URLs or server details; present bounded text only.
            throw LocalDashboardError.connectionFailed
        }
    }
}

private final class NoDashboardRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}
