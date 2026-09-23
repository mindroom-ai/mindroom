import AppKit
import XCTest
@testable import MindRoom

final class LocalAgentsPresentationTests: XCTestCase {
    func testOnlyAnInstalledStoppedServiceOffersStart() {
        XCTAssertEqual(MindRoomServiceState.stopped.primaryAction, .startService)
        XCTAssertEqual(MindRoomServiceState.running.primaryAction, .stopService)
        XCTAssertEqual(MindRoomServiceState.runtimeMissing.primaryAction, .installRuntime)
        XCTAssertEqual(MindRoomServiceState.notInstalled.primaryAction, .installService)
        XCTAssertNil(MindRoomServiceState.unknown.primaryAction)
    }

    func testDashboardRequiresRunningService() {
        XCTAssertTrue(MindRoomServiceState.running.canOpenDashboard)
        for state: MindRoomServiceState in [.stopped, .notInstalled, .runtimeMissing, .unknown] {
            XCTAssertFalse(state.canOpenDashboard)
        }
    }

    func testOnlyLoginItemLaunchStaysInMenuBar() {
        XCTAssertTrue(AppLaunchPolicy.shouldShowWindow(launchEvent: nil))
        let event = NSAppleEventDescriptor(
            eventClass: AEEventClass(kCoreEventClass), eventID: AEEventID(kAEOpenApplication),
            targetDescriptor: nil, returnID: AEReturnID(kAutoGenerateReturnID),
            transactionID: AETransactionID(kAnyTransactionID)
        )
        XCTAssertTrue(AppLaunchPolicy.shouldShowWindow(launchEvent: event))
        event.setParam(NSAppleEventDescriptor(enumCode: OSType(keyAELaunchedAsLogInItem)), forKeyword: AEKeyword(keyAEPropData))
        XCTAssertFalse(AppLaunchPolicy.shouldShowWindow(launchEvent: event))
    }
}
