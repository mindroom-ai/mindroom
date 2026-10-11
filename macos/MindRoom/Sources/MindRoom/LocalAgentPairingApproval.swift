import Foundation

struct LocalAgentPairingApproval: Equatable {
    let url: URL
    let code: String

    static func parse(_ output: String) -> Self? {
        // The CLI prints a non-wrapped URL on its own line. Ignore a partial pipe read.
        for line in output.components(separatedBy: "\n").dropLast().reversed() {
            let text = line.trimmingCharacters(in: .whitespacesAndNewlines)
            guard let url = URL(string: text), WebNavigationPolicy.isWebURL(url),
                  let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
                  let code = components.queryItems?.first(where: { $0.name == "code" })?.value,
                  code.range(of: #"^[A-Z0-9]{4}-[A-Z0-9]{4}$"#, options: .regularExpression) != nil else { continue }
            return Self(url: url, code: code)
        }
        return nil
    }
}
