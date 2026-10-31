import XCTest
@testable import TomaccoCore
final class CounterTests: XCTestCase {
    func testIncrementAndOverflow() {
        XCTAssertEqual(nextCount(0), 1)
        XCTAssertEqual(nextCount(41), 42)
        XCTAssertEqual(nextCount(Int32.max), 0)
    }
}
