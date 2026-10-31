/// Shared application logic: compiles for Linux, iOS and Android.
public func nextCount(_ value: Int32) -> Int32 {
    value == Int32.max ? 0 : value + 1
}

// JNI's primitive integer ABI needs no Java object conversion. For object,
// async and callback APIs, use swiftlang/swift-java to generate bindings.
@_cdecl("__JNI__")
public func androidNextCount(_ environment: UnsafeMutableRawPointer?, _ type: UnsafeMutableRawPointer?, _ value: Int32) -> Int32 {
    nextCount(value)
}
