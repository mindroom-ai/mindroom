import XCTest
@testable import MindRoom

final class InstalledApplicationSearchTests: XCTestCase {
    private let chrome = InstalledDesktopApplication(
        id: "com.google.Chrome",
        name: "Google Chrome",
        running: false
    )

    func testMatchesEitherNameWordOrPartialWord() {
        for query in ["Google", "Chrome", "goo", "chr", "hrom"] {
            XCTAssertTrue(chrome.matches(search: query), query)
        }
    }

    func testMatchesReorderedPartialNameWords() {
        for query in ["goo chr", "chr goo", "Chrome Google"] {
            XCTAssertTrue(chrome.matches(search: query), query)
        }
    }

    func testIgnoresCaseAndExtraWhitespace() {
        XCTAssertTrue(chrome.matches(search: "  CHR\t\nGOO  "))
    }

    func testIgnoresDiacritics() {
        let application = InstalledDesktopApplication(id: "org.example.editor", name: "Café Éditeur", running: false)

        XCTAssertTrue(application.matches(search: "editeur cafe"))
        XCTAssertTrue(application.matches(search: "ÉDITEUR CAFÉ"))
    }

    func testMatchesFuzzyCharacterSubsequences() {
        for query in ["chrm", "gchr", "chrm ggl"] {
            XCTAssertTrue(chrome.matches(search: query), query)
        }
    }

    func testMatchesBundleIdentifier() {
        let application = InstalledDesktopApplication(id: "org.mozilla.firefox", name: "Browser", running: false)

        for query in ["org.mozilla.firefox", "mozilla", "fire moz", "browser moz", "FIRE MOZ"] {
            XCTAssertTrue(application.matches(search: query), query)
        }
    }

    func testDoesNotFuzzyMatchBundleIdentifier() {
        let application = InstalledDesktopApplication(id: "org.mozilla.firefox", name: "Browser", running: false)

        XCTAssertFalse(application.matches(search: "frfx"))
    }

    func testChromeSearchDoesNotMatchUnrelatedBundleIdentifierSubsequences() {
        let applications = [
            InstalledDesktopApplication(id: "com.google.drivefs.shortcuts.docs", name: "Google Docs", running: false),
            InstalledDesktopApplication(id: "com.google.drivefs.shortcuts.sheets", name: "Google Sheets", running: false),
            InstalledDesktopApplication(id: "com.github.xor-gate.syncthing-macosx", name: "Syncthing", running: false),
        ]

        for application in applications {
            XCTAssertFalse(application.matches(search: "chr goo"), application.name)
        }
    }

    func testEmptyOrWhitespaceOnlyQueryMatches() {
        for query in ["", " \t\n "] {
            XCTAssertTrue(chrome.matches(search: query), query)
        }
    }

    func testRequiresEveryQueryTokenToMatch() {
        for query in ["Safari", "chrome safari", "google zz"] {
            XCTAssertFalse(chrome.matches(search: query), query)
        }
    }

    func testFuzzyMatchingPreservesCharacterOrderAndCount() {
        for query in ["emorhc", "chromee", "gggg"] {
            XCTAssertFalse(chrome.matches(search: query), query)
        }
    }

    func testDoesNotJoinNameAndIdentifierForOneFuzzyToken() {
        let application = InstalledDesktopApplication(id: "org.example.browser", name: "Chrome", running: false)

        XCTAssertFalse(application.matches(search: "chromeorg"))
    }
}
