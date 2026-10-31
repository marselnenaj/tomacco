// Native Swift compilation runs directly from Gradle; no Python or Tomacco
// installation is required to build this generated project outside the studio.
listOf("debug", "release").forEach { variant ->
    val capitalized = variant.replaceFirstChar { it.uppercaseChar() }
    val swiftBuild = tasks.register("buildSwift$capitalized") {
        doLast {
            val root = rootProject.projectDir
            @Suppress("UNCHECKED_CAST")
            val document = groovy.json.JsonSlurper().parse(root.resolve("tomacco.json")) as Map<String, Any>
            @Suppress("UNCHECKED_CAST")
            val config = document["swift"] as Map<String, Any>
            val swift = System.getenv("SWIFT_EXECUTABLE") ?: "swift"
            val sdk = System.getenv("SWIFT_ANDROID_SDK") ?: config["sdk"].toString()
            val ndk = file(System.getenv("ANDROID_NDK_HOME") ?: error("Set ANDROID_NDK_HOME to your Swift SDK's required NDK."))
            check(ndk.resolve("source.properties").isFile) { "Invalid Android NDK: $ndk" }
            fun execute(args: List<String>, capture: Boolean = false): String {
                val builder = ProcessBuilder(args).directory(root)
                if (capture) builder.redirectError(ProcessBuilder.Redirect.INHERIT) else builder.inheritIO()
                val process = builder.start()
                val output = if (capture) process.inputStream.bufferedReader().use { it.readText() } else ""
                check(process.waitFor() == 0) { "Swift command failed: $args" }
                return output
            }
            val architectures = mapOf("arm64-v8a" to Pair("aarch64", "aarch64-linux-android"), "x86_64" to Pair("x86_64", "x86_64-linux-android"))
            for (abi in config["abis"] as List<*>) {
                val (arch, ndkArch) = architectures[abi] ?: error("Unsupported Swift Android ABI: $abi")
                val triple = "$arch-unknown-linux-android${config["api"]}"
                val properties = execute(listOf(swift, "sdk", "configure", "--show-configuration", sdk, triple), true)
                    .lineSequence().mapNotNull { line -> line.split(": ", limit = 2).takeIf { it.size == 2 }?.let { it[0] to it[1] } }.toMap()
                check(file(properties.getValue("sdkRootPath")).resolve("usr/include").isDirectory) { "Connect the Swift SDK to the NDK with swift.android_configure first." }
                val args = listOf(swift, "build", "--package-path", root.resolve(config["package"].toString()).path,
                    "--swift-sdk", sdk, "--triple", triple, "--configuration", variant,
                    "-Xswiftc", "-static-stdlib", "-Xswiftc", "-resource-dir", "-Xswiftc", properties.getValue("swiftStaticResourcesPath"))
                execute(args)
                val bin = file(execute(args + "--show-bin-path", true).trim())
                val libraries = bin.listFiles()?.filter { it.isFile && it.extension == "so" }.orEmpty()
                check(libraries.isNotEmpty()) { "Swift produced no shared library for $abi" }
                val runtime = ndk.resolve("toolchains/llvm/prebuilt").listFiles()?.map { it.resolve("sysroot/usr/lib/$ndkArch/libc++_shared.so") }?.firstOrNull { it.isFile }
                    ?: error("NDK C++ runtime missing for $abi")
                val destination = layout.buildDirectory.dir("generated/swift/$variant/jniLibs/$abi").get().asFile
                destination.deleteRecursively()
                check(destination.mkdirs()) { "Cannot create $destination" }
                (libraries + runtime).forEach { it.copyTo(destination.resolve(it.name), overwrite = true) }
                logger.lifecycle("Packaged native Swift library for $abi")
            }
        }
    }
    android.sourceSets.getByName(variant).jniLibs.srcDir("build/generated/swift/$variant/jniLibs")
    tasks.configureEach {
        if (name == "merge${capitalized}JniLibFolders") dependsOn(swiftBuild)
    }
}
