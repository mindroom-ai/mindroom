import AppKit
import XCTest
@testable import MindRoom

final class BrandResourceTests: XCTestCase {
    func testColoredMenuLogoHasStandardAndRetinaRepresentations() {
        let image = MindRoomBrand.menuImage
        XCTAssertTrue(image.isValid)
        XCTAssertFalse(image.isTemplate)
        XCTAssertEqual(image.size, NSSize(width: 20, height: 20))
        XCTAssertEqual(image.representations.map(\.pixelsWide).sorted(), [20, 40])
        XCTAssertTrue(image.representations.allSatisfy { $0.size == image.size })
    }

    func testBundledSVGDecodesWithoutNetworkOrSourceCheckout() throws {
        let url = try XCTUnwrap(MindRoomBrand.logoURL)
        XCTAssertEqual(url.pathExtension, "svg")
        let image = try XCTUnwrap(NSImage(contentsOf: url))
        XCTAssertGreaterThan(image.size.width, 0)
        XCTAssertGreaterThan(image.size.height, 0)
        XCTAssertNotNil(image.tiffRepresentation)
        let rasterURL = try XCTUnwrap(MindRoomBrand.imageURL(in: .main))
        XCTAssertEqual(rasterURL.pathExtension, "png")
        XCTAssertNotNil(NSImage(contentsOf: rasterURL)?.tiffRepresentation)
    }

    func testPackagedAppLoadsItsOwnLogoAndDoesNotFallBackToCheckout() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let app = directory.appendingPathComponent("MindRoom.app")
        let resources = app.appendingPathComponent("Contents/Resources/MindRoom_MindRoom.bundle")
        try FileManager.default.createDirectory(at: resources, withIntermediateDirectories: true)
        let info: [String: String] = ["CFBundleIdentifier": "chat.mindroom.logo-test", "CFBundlePackageType": "APPL"]
        let data = try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
        try data.write(to: app.appendingPathComponent("Contents/Info.plist"))
        let source = try XCTUnwrap(MindRoomBrand.logoURL)
        let destination = resources.appendingPathComponent("logo.svg")
        try FileManager.default.copyItem(at: source, to: destination)
        for name in ["logo", "logo-menu", "logo-menu@2x"] {
            let rasterSource = try XCTUnwrap(MindRoomBrand.imageURL(in: .main, name: name))
            let rasterDestination = resources.appendingPathComponent("\(name).png")
            try FileManager.default.copyItem(at: rasterSource, to: rasterDestination)
        }
        let bundle = try XCTUnwrap(Bundle(url: app))
        XCTAssertEqual(MindRoomBrand.logoURL(in: bundle)?.standardizedFileURL, destination.standardizedFileURL)
        for name in ["logo", "logo-menu", "logo-menu@2x"] {
            let rasterDestination = resources.appendingPathComponent("\(name).png")
            XCTAssertEqual(MindRoomBrand.imageURL(in: bundle, name: name)?.standardizedFileURL, rasterDestination.standardizedFileURL)
        }

        let missingApp = directory.appendingPathComponent("Missing.app")
        try FileManager.default.createDirectory(at: missingApp.appendingPathComponent("Contents/Resources"), withIntermediateDirectories: true)
        try data.write(to: missingApp.appendingPathComponent("Contents/Info.plist"))
        XCTAssertNil(MindRoomBrand.logoURL(in: try XCTUnwrap(Bundle(url: missingApp))))
        XCTAssertNil(MindRoomBrand.imageURL(in: try XCTUnwrap(Bundle(url: missingApp))))
    }
}
