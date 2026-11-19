import Foundation
import Security

/// Decides whether an app bundle is the MindRoom Chat that MindRoom may install or open.
enum ChatAppSignature {
    static let bundleIdentifier = "chat.mindroom.app"

    /// The Developer ID build of MindRoom Chat, signed by MindRoom's team and notarized by Apple.
    /// A copy MindRoom downloads carries no quarantine flag, so macOS does not check it at first
    /// launch: this requirement is the only gate, whatever the Mac's Gatekeeper setting.
    static let requirement = "\(chatApp) and \(developerID) and notarized"

    /// A copy that is already on this Mac may also be the Mac App Store's build: this is the app's
    /// own designated requirement.
    static let installedRequirement =
        "\(chatApp) and (certificate leaf[field.1.2.840.113635.100.6.1.9] exists or \(developerID))"

    private static let chatApp = #"anchor apple generic and identifier "chat.mindroom.app""#
    private static let developerID = """
    certificate 1[field.1.2.840.113635.100.6.2.6] exists \
    and certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf[subject.OU] = "DNA6966LGZ"
    """
    private static let wholeBundle = kSecCSCheckAllArchitectures | kSecCSCheckNestedCode

    /// Throws unless the whole bundle's signature is intact, has no link out of the bundle, passes
    /// a revocation check, and satisfies `requirement`.
    static func verify(_ app: URL) throws {
        let flags = SecCSFlags(rawValue: wholeBundle | kSecCSStrictValidate | kSecCSRestrictSymlinks)
        try check(app, requirement, flags.union(.enforceRevocationChecks))
    }

    /// Throws unless `app` is MindRoom Chat as MindRoom or the Mac App Store ships it. Not strict:
    /// an installed copy may have gained Finder attributes, and a click must not wait on the network.
    static func verifyInstalled(_ app: URL) throws {
        try check(app, installedRequirement, SecCSFlags(rawValue: wholeBundle))
    }

    private static func check(_ app: URL, _ requirement: String, _ flags: SecCSFlags) throws {
        var code: SecStaticCode?
        var required: SecRequirement?
        var status = SecStaticCodeCreateWithPath(app as CFURL, [], &code)
        if status == errSecSuccess {
            status = SecRequirementCreateWithString(requirement as CFString, [], &required)
        }
        if status == errSecSuccess, let code {
            status = SecStaticCodeCheckValidity(code, flags, required)
        }
        guard status == errSecSuccess else {
            throw ChatAppInstallError.untrusted(SecCopyErrorMessageString(status, nil) as String? ?? "error \(status)")
        }
    }
}
