import AppKit

enum AppLaunchPolicy {
    static func shouldShowWindow(launchEvent: NSAppleEventDescriptor?) -> Bool {
        launchEvent?.paramDescriptor(forKeyword: AEKeyword(keyAEPropData))?.enumCodeValue
            != OSType(keyAELaunchedAsLogInItem)
    }
}
