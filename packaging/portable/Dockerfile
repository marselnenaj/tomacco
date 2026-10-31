# The build environment of the portable package (tools/build_portable.py).
#
# Ubuntu 22.04 has glibc 2.35, so everything compiled here runs on glibc 2.35
# and newer (Ubuntu 22.04, Debian 12 and Fedora 36 or later). Qt comes from the official
# Qt binaries (built on RHEL 9.6, glibc 2.34) and FFmpeg is built here as an
# LGPL-only, decoder-only library. Every download is checked against a pinned
# SHA-256.
FROM ubuntu:22.04@sha256:076e7bf82319619f28dd0aa9c02ab34b055ad37100395b4f49a2ffba04fb5e11

ARG DEBIAN_FRONTEND=noninteractive
# Compilers and build tools, the development files Qt's CMake packages look
# for (OpenGL, xkbcommon), and the xcb helper libraries the package bundles.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils p7zip-full patchelf binutils file \
        g++-12 gcc-12 make pkg-config ninja-build python3 nasm protobuf-compiler \
        libgl-dev libegl-dev libopengl-dev libxkbcommon-dev libfontconfig1-dev libfreetype-dev \
        libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-util1 \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-12 100 \
    && update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-12 100 \
    && update-alternatives --install /usr/bin/cc cc /usr/bin/gcc-12 100 \
    && update-alternatives --install /usr/bin/c++ c++ /usr/bin/g++-12 100

# CMake: Qt 6.12 requires 3.25 or newer, Ubuntu 22.04 has 3.22. Kitware's
# release binary, checked against its published SHA-256.
ARG CMAKE_VERSION=4.4.5
ARG CMAKE_SHA256=ed85135c5a5135b45e50423cb406dea04ca3338da0b8ef3c9ede963559680be0
RUN curl -fsSL -o /tmp/cmake.tar.gz "https://github.com/Kitware/CMake/releases/download/v${CMAKE_VERSION}/cmake-${CMAKE_VERSION}-linux-x86_64.tar.gz" \
    && echo "${CMAKE_SHA256}  /tmp/cmake.tar.gz" | sha256sum -c - \
    && mkdir /opt/cmake && tar -xzf /tmp/cmake.tar.gz -C /opt/cmake --strip-components=1 \
    && rm -rf /tmp/cmake.tar.gz /opt/cmake/doc /opt/cmake/man
ENV PATH=/opt/cmake/bin:$PATH

# Rust: rustup's installer at a pinned version and checksum, then the pinned
# toolchain (Cargo.toml's rust-version), minimal profile.
ARG RUSTUP_VERSION=1.29.2
ARG RUSTUP_SHA256=5be191e8c70a5007074254cb74d78dbfdd25cd5a61c27de7b8e5410122ec24f4
ARG RUST_VERSION=1.89.0
ENV RUSTUP_HOME=/opt/rustup
RUN curl -fsSL -o /tmp/rustup-init "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/x86_64-unknown-linux-gnu/rustup-init" \
    && echo "${RUSTUP_SHA256}  /tmp/rustup-init" | sha256sum -c - \
    && chmod +x /tmp/rustup-init \
    && CARGO_HOME=/opt/cargo /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain "${RUST_VERSION}" \
    && rm -rf /tmp/rustup-init /opt/cargo/registry \
    && chmod -R a+rX /opt/rustup /opt/cargo
# The toolchain's own directory, so cargo needs no writable rustup state.
ENV PATH=/opt/rustup/toolchains/${RUST_VERSION}-x86_64-unknown-linux-gnu/bin:$PATH

# FFmpeg: the release tarball (its signature by the FFmpeg release key was
# checked when the checksum was pinned), built as shared libraries with only
# what the desktop uses: the H.264 and MJPEG decoders and parsers, and
# libswscale. No --enable-gpl, --enable-version3 or --enable-nonfree, and no
# external libraries, so the result is LGPL-2.1-or-later.
ARG FFMPEG_VERSION=9.0.2
ARG FFMPEG_SHA256=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
RUN curl -fsSL -o /tmp/ffmpeg.tar.xz "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" \
    && echo "${FFMPEG_SHA256}  /tmp/ffmpeg.tar.xz" | sha256sum -c - \
    && mkdir /tmp/ffmpeg && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg --strip-components=1 \
    && cd /tmp/ffmpeg \
    && ./configure --prefix=/opt/ffmpeg --enable-shared --disable-static --disable-programs --disable-doc \
        --disable-autodetect --disable-everything --disable-network \
        --disable-avdevice --disable-avformat --disable-avfilter --disable-swresample \
        --enable-decoder=h264,mjpeg --enable-parser=h264,mjpeg \
    && grep -q '^#define CONFIG_GPL 0' config.h && grep -q '^#define CONFIG_NONFREE 0' config.h \
    && grep -q '^#define CONFIG_VERSION3 0' config.h \
    && make -j"$(nproc)" && make install \
    && mkdir -p /opt/ffmpeg/share/licenses \
    && cp COPYING.LGPLv2.1 COPYING.LGPLv3 LICENSE.md /opt/ffmpeg/share/licenses/ \
    && cp ffbuild/config.mak /opt/ffmpeg/share/licenses/config.mak \
    && cd / && rm -rf /tmp/ffmpeg /tmp/ffmpeg.tar.xz

# Qt: the pinned archives of the official binaries, unpacked as Qt's own
# installer does (ICU's libraries go into lib/), without the WebDriver and the
# static libraries of the QML tooling, which the build does not use.
COPY qt-archives.txt /tmp/qt-archives.txt
RUN mkdir -p /opt/qt /tmp/qt-download \
    && grep -v '^#' /tmp/qt-archives.txt | while read -r sum path; do \
        [ -n "$sum" ] || continue; \
        file="/tmp/qt-download/$(basename "$path")"; \
        curl -fsSL --retry 3 -o "$file" "https://download.qt.io/online/qtsdkrepository/linux_x64/$path" \
        && echo "$sum  $file" | sha256sum -c - \
        && case "$file" in *icu-linux*) 7z x -y -bd -o/opt/qt/lib "$file" >/dev/null ;; *) 7z x -y -bd -o/opt/qt "$file" >/dev/null ;; esac \
        && rm -f "$file" || exit 1; \
    done \
    && rm -rf /tmp/qt-download /opt/qt/libexec/webenginedriver /opt/qt/doc \
        /opt/qt/lib/libQt6QmlLS.a /opt/qt/lib/libQt6QmlDom.a /opt/qt/lib/libQt6QmlLint.a /opt/qt/lib/libQt6Qmltc.a \
    && test -f /opt/qt/lib/cmake/Qt6WebEngineQuick/Qt6WebEngineQuickConfig.cmake \
    && test -e /opt/qt/lib/libicuuc.so.73

# The host libraries Qt links against (NSS, ALSA, D-Bus, X11, Wayland, GBM),
# for the linker only: the package expects them from the system.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libasound2 libdbus-1-3 libglib2.0-0 libzstd1 libudev1 libexpat1 libgbm1 libbrotli1 libgssapi-krb5-2 \
        libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 libxtst6 libxkbfile1 \
        libxcb-dri3-0 libwayland-client0 libwayland-cursor0 libwayland-egl1 \
    && rm -rf /var/lib/apt/lists/*

# ICU's license, for the copy of ICU 73.2 that comes with Qt's binaries.
ARG ICU_LICENSE_SHA256=f3005e195ff74d8812cc1f182a1c446fab678d70a10e3dada497585befee5416
RUN mkdir -p /opt/qt/licenses \
    && curl -fsSL -o /opt/qt/licenses/LICENSE.ICU "https://raw.githubusercontent.com/unicode-org/icu/release-73-2/icu4c/LICENSE" \
    && echo "${ICU_LICENSE_SHA256}  /opt/qt/licenses/LICENSE.ICU" | sha256sum -c -
