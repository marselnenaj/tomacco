# Device capability replies

`reims-device-capabilities.patch` makes two advertised capabilities agree with
the operations the engine actually implements. It is applied after the four
graphics translation patches by `tools/build_ios_paravirt.py`.

## Heap textures

Device-info key 16 enables texture placement in a Metal heap. The guest's
`supportsResourceHeaps` combines keys 14 and 16 with a protocol-version feature.
The older version-4 path kept that feature off; the current iOS path negotiates
version 62 and uses it.

The heap-placement provider now supplies key 16's availability. Linux has no
provider for the guest's Metal heap size/alignment contract, so it explicitly
answers zero. macOS can query its Metal driver on either rendering backend.
Vulkan image memory requirements are not substituted for the guest heap ABI.
Key 14, heap-buffer granularity, and the protocol's dual-plane bit are preserved.
Individual unsupported heap requests still return their original typed refusal.

## Texture-write rounding

Key 41 is a bitmask indexed by the requested rounding mode: 0 is device default,
1 is toward zero, and 2 is nearest even. If the key is absent, the guest assumes
all three are supported. Sending an explicit mask of 1 is therefore necessary
while the engine implements device-default texture conversion only.

The classic render descriptor carries this ordinal in tag `0x1e`; the compute
descriptor uses tag `0x0a`. Both are four-byte fields. The decoders accept an
explicit default and distinguish unsupported, unknown, and malformed modes.
They do not silently treat requested nearest-even conversion as device default.
The mesh serializer has a separate property list and is not changed.

The protocol declarations were recovered from the guest's capability consumer and
descriptor serializers. No extracted implementation, disassembly, shader, or firmware is
included. These changes correct capability reporting; they do not implement heap textures
or explicit rounding modes.

The Linux Vulkan tests cover the resource descriptors and the register and capability
replies. The rounding cases compare advertised mode bits with both decoders, preserve
explicit-default behavior, and refuse malformed fields and unimplemented modes.
