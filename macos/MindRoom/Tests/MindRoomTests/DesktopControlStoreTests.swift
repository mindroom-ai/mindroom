import XCTest
@testable import MindRoom

@MainActor
final class DesktopControlStoreTests: XCTestCase {
    func testUnconfirmedSaveClearsRecoveryFromPreviousImportError() {
        let store = DesktopControlStore()
        store.setupDescriptor = "not valid JSON"
        store.importSetupDescriptor()
        XCTAssertNotNil(store.recovery)

        store.saveConfiguration()

        XCTAssertEqual(store.errorMessage, "Confirm the displayed controller, requester, and agent before saving.")
        XCTAssertNil(store.recovery)
    }
}
