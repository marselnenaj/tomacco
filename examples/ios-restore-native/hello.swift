@_silgen_name("tomacco_write")
func tomaccoWrite(_ bytes: UnsafePointer<UInt8>, _ count: Int) -> Int

@_cdecl("tomacco_swift_main")
public func tomaccoSwiftMain(_ count: Int) -> Int32 {
    guard count >= 0 && count <= 1000 else { return 7 }
    let values = UnsafeMutableBufferPointer<Int>.allocate(capacity: count)
    defer { values.deallocate() }
    for index in values.indices { values[index] = index + 1 }
    var total = 0
    for value in values { total += value }
    guard total == count * (count + 1) / 2 else { return 8 }

    let message: StaticString = "Embedded Swift compiled on Linux: Grüße 日本語 😀\n"
    return tomaccoWrite(message.utf8Start, message.utf8CodeUnitCount) == message.utf8CodeUnitCount ? 0 : 9
}
