fn main() {
    println!("cargo:rerun-if-changed=native/protocol/idb.proto");
    tonic_prost_build::configure()
        .build_server(true)
        .compile_protos(&["native/protocol/idb.proto"], &["native/protocol"])
        .expect("Compile the iOS runtime protocol; install protobuf/protoc if missing");
}
