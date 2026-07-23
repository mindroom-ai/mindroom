// swift-tools-version: 5.9

import PackageDescription

let package = Package(
    name: "MindRoom",
    platforms: [
        .macOS(.v13),
    ],
    products: [
        .executable(name: "MindRoom", targets: ["MindRoom"]),
    ],
    dependencies: [
        .package(url: "https://github.com/sparkle-project/Sparkle", exact: "2.9.2"),
    ],
    targets: [
        .executableTarget(
            name: "MindRoom",
            dependencies: [
                .product(name: "Sparkle", package: "Sparkle"),
            ],
            path: "Sources/MindRoom"
        ),
        .testTarget(
            name: "MindRoomTests",
            dependencies: ["MindRoom"],
            path: "Tests/MindRoomTests"
        ),
    ]
)
