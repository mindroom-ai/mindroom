import Foundation

extension DesktopStatus {
    var canStopBridge: Bool {
        bridge.state != "stopped" || helper.state == "starting"
    }
}
