import AppKit
import SwiftUI

enum MindRoomBrand {
    static var logoURL: URL? { logoURL(in: .main) }

    static func logoURL(in applicationBundle: Bundle) -> URL? {
        resourceURL(extension: "svg", in: applicationBundle)
    }

    static func imageURL(in applicationBundle: Bundle, name: String = "logo") -> URL? {
        resourceURL(name: name, extension: "png", in: applicationBundle)
    }

    private static func resourceURL(name: String = "logo", extension fileExtension: String, in applicationBundle: Bundle) -> URL? {
        if applicationBundle.bundleURL.pathExtension == "app" {
            guard let resourceURL = applicationBundle.resourceURL?
                .appendingPathComponent("MindRoom_MindRoom.bundle"),
                  let resources = Bundle(url: resourceURL) else { return nil }
            return resources.url(forResource: name, withExtension: fileExtension)
        }
        return Bundle.module.url(forResource: name, withExtension: fileExtension)
    }

    static var image: NSImage {
        // AppKit omits some SVG masks; use the official PNG companion for identical shading.
        guard let url = imageURL(in: .main), let image = NSImage(contentsOf: url) else {
            return NSImage(systemSymbolName: "m.square", accessibilityDescription: "MindRoom")!
        }
        return image
    }

    static var menuImage: NSImage {
        let image = NSImage(size: NSSize(width: 20, height: 20))
        for name in ["logo-menu", "logo-menu@2x"] {
            guard let url = imageURL(in: .main, name: name),
                  let data = try? Data(contentsOf: url),
                  let representation = NSBitmapImageRep(data: data) else { continue }
            representation.size = image.size
            image.addRepresentation(representation)
        }
        // Template rendering discards the interior shading and leaves a solid silhouette.
        image.isTemplate = false
        return image
    }
}

struct MindRoomLogo: View {
    var body: some View {
        Image(nsImage: MindRoomBrand.image)
            .resizable()
            .interpolation(.high)
            .scaledToFit()
            .accessibilityLabel("MindRoom logo")
    }
}
