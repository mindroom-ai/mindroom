import Foundation
import Security

/// Decides whether an app bundle is the MindRoom Chat that MindRoom may install.
enum ChatAppSignature {
    static let bundleIdentifier = "chat.mindroom.app"

    /// The Developer ID build of MindRoom Chat, signed by MindRoom's team and notarized by Apple.
    /// A copy MindRoom downloads carries no quarantine flag, so macOS does not check it at first
    /// launch: this requirement is the only gate, whatever the Mac's Gatekeeper setting.
    static let requirement = """
    anchor apple generic and identifier "chat.mindroom.app" \
    and certificate 1[field.1.2.840.113635.100.6.2.6] exists \
    and certificate leaf[field.1.2.840.113635.100.6.1.13] exists \
    and certificate leaf[subject.OU] = "DNA6966LGZ" and notarized
    """

    /// Throws unless the whole bundle's signature is intact, unrevoked, and satisfies `requirement`.
    static func verify(_ app: URL) throws {
        var code: SecStaticCode?
        var required: SecRequirement?
        var status = SecStaticCodeCreateWithPath(app as CFURL, [], &code)
        if status == errSecSuccess {
            status = SecRequirementCreateWithString(requirement as CFString, [], &required)
        }
        if status == errSecSuccess, let code {
            let flags = SecCSFlags(rawValue: kSecCSCheckAllArchitectures | kSecCSCheckNestedCode | kSecCSStrictValidate)
                .union(.enforceRevocationChecks)
            status = SecStaticCodeCheckValidity(code, flags, required)
        }
        guard status == errSecSuccess else {
            throw ChatAppInstallError.untrusted(SecCopyErrorMessageString(status, nil) as String? ?? "error \(status)")
        }
    }
}
