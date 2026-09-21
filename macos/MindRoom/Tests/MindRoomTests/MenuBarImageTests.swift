import AppKit
import XCTest
@testable import MindRoom

@MainActor
final class MenuBarImageTests: XCTestCase {
    func testMenuBarTemplateLoadsWithoutAnInstalledAppBundle() throws {
        let image = try XCTUnwrap(menuBarImage())

        XCTAssertTrue(image.isValid)
        XCTAssertTrue(image.isTemplate)
        XCTAssertEqual(image.size, NSSize(width: 18, height: 18))
        XCTAssertNotNil(image.tiffRepresentation)
    }
}
