# A headless Tomacco image for coding agents, servers and CI: tomaccoctl (the
# JSON command line and MCP server) with a JDK, Gradle and the Android SDK.
# No desktop, no virtual iPhone, no iPhone USB tools. docs/container.md has the
# details; tools/build_container.py builds and checks it.
#
#   docker build --build-arg ACCEPT_ANDROID_SDK_LICENSE=1 -t tomacco packaging/container
#   docker build --build-arg ACCEPT_ANDROID_SDK_LICENSE=1 --build-arg ANDROID_EMULATOR=1 -t tomacco:emulator packaging/container
#   docker run -i --rm tomacco mcp
#
# Building it installs the Android SDK, which is under Google's Android
# Software Development Kit License Agreement
# (https://developer.android.com/studio/terms). The build refuses to start
# unless ACCEPT_ANDROID_SDK_LICENSE=1 says that whoever builds it accepts it.
#
# Ubuntu 24.04 has glibc 2.39: newer than the portable package's floor (2.35)
# and enough for the regular package's tomaccoctl, which is the smaller download.
FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852

ARG ACCEPT_ANDROID_SDK_LICENSE=0
RUN if [ "$ACCEPT_ANDROID_SDK_LICENSE" != 1 ]; then \
        echo "This image contains the Android SDK. Building it means you accept Google's Android SDK license" >&2; \
        echo "(https://developer.android.com/studio/terms). Build again with --build-arg ACCEPT_ANDROID_SDK_LICENSE=1." >&2; \
        exit 1; \
    fi

ARG DEBIAN_FRONTEND=noninteractive
# The JDK for Gradle and the SDK tools, the tools tomaccoctl calls (curl,
# unzip, openssl, git), and tini as process 1, which reaps the job workers'
# children.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl unzip openssl git tini openjdk-17-jdk-headless \
    && rm -rf /var/lib/apt/lists/*

# tomaccoctl from a signed release package. The package is checked against its
# .sha256 and against its Ed25519 signature with the release key below (the
# same as native/product.rs and install.sh); nothing is installed otherwise.
# A package placed in the build context's packages/ directory (as
# tools/build_container.py does for a release being made) is used instead of
# the download, and checked the same way.
ARG TOMACCO_VERSION=1.3.0
# regular: the Arch build (glibc 2.39, 7 MB); portable: the Ubuntu 22.04 build
# (glibc 2.35, 177 MB, of which only tomaccoctl is used here).
ARG TOMACCO_PACKAGE=regular
RUN --mount=type=bind,target=/context \
    set -eu; \
    public_keys="eUZ5PV5fqziL3khcVKTg8wiIbOYeIggiMD6mWLkEYwM="; \
    case "$TOMACCO_PACKAGE" in \
        regular) name="tomacco-${TOMACCO_VERSION}-Linux-x86_64" ;; \
        portable) name="tomacco-${TOMACCO_VERSION}-Linux-x86_64-portable" ;; \
        *) echo "TOMACCO_PACKAGE is regular or portable" >&2; exit 1 ;; \
    esac; \
    work="$(mktemp -d)"; cd "$work"; \
    for file in "$name.tar.gz" "$name.tar.gz.sha256" "$name.tar.gz.sig"; do \
        if [ -f "/context/packages/$file" ]; then cp "/context/packages/$file" .; \
        else curl -fsSL --retry 3 -o "$file" "https://github.com/marselnenaj/tomacco/releases/download/v${TOMACCO_VERSION}/$file"; fi; \
    done; \
    sha256sum --check --strict "$name.tar.gz.sha256"; \
    base64 -d "$name.tar.gz.sig" > signature; \
    verified=; \
    for public_key in $public_keys; do \
        { printf '\060\052\060\005\006\003\053\145\160\003\041\000'; printf '%s' "$public_key" | base64 -d; } > key.der; \
        if openssl pkeyutl -verify -rawin -pubin -keyform DER -inkey key.der -in "$name.tar.gz" -sigfile signature >/dev/null 2>&1; then verified=1; fi; \
    done; \
    if [ -z "$verified" ]; then echo "$name.tar.gz: the signature matches no release key" >&2; exit 1; fi; \
    tar -xzf "$name.tar.gz" "$name/bin/tomaccoctl" "$name/share/doc/tomacco" "$name/share/man/man1/tomaccoctl.1"; \
    install -D -m 0755 "$name/bin/tomaccoctl" /usr/local/bin/tomaccoctl; \
    install -D -m 0644 "$name/share/man/man1/tomaccoctl.1" /usr/local/share/man/man1/tomaccoctl.1; \
    mkdir -p /usr/share/doc/tomacco; cp -R "$name/share/doc/tomacco/." /usr/share/doc/tomacco/; \
    sha256sum "$name.tar.gz" > /usr/share/doc/tomacco/PACKAGE.sha256; \
    cd /; rm -rf "$work"; \
    tomaccoctl --version

# Gradle, which the Android starters need (they have no wrapper), checked
# against the SHA-256 Gradle publishes. The starter was verified with 9.3.1.
ARG GRADLE_VERSION=9.3.1
ARG GRADLE_SHA256=b266d5ff6b90eada6dc3b20cb090e3731302e553a27c5d3e4df1f0d76beaff06
RUN curl -fsSL --retry 3 -o /tmp/gradle.zip "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" \
    && echo "${GRADLE_SHA256}  /tmp/gradle.zip" | sha256sum -c - \
    && unzip -q /tmp/gradle.zip -d /opt \
    && mv "/opt/gradle-${GRADLE_VERSION}" /opt/gradle \
    && ln -s /opt/gradle/bin/gradle /usr/local/bin/gradle \
    && rm /tmp/gradle.zip

# The user the image runs as. Ubuntu's image has a user `ubuntu` with UID
# 1000; it makes way for `tomacco` with the same UID.
RUN userdel --remove ubuntu 2>/dev/null || true; \
    groupadd --gid 1000 tomacco \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash tomacco \
    && mkdir -p /data /opt/android-sdk \
    && chown tomacco:tomacco /data /opt/android-sdk

ENV ANDROID_HOME=/opt/android-sdk \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
    PATH=/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools:/opt/android-sdk/emulator:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    TOMACCO_HOME=/data/tomacco \
    TOMACCO_UPDATES=off \
    GRADLE_USER_HOME=/data/gradle \
    ANDROID_USER_HOME=/data/android \
    ANDROID_AVD_HOME=/data/android/avd \
    ANDROID_EMULATOR_HOME=/data/android

USER tomacco
WORKDIR /home/tomacco

# The Android command-line tools at a pinned version: the archive must match
# the SHA-1 in Google's repository index (the only digest Google publishes,
# the check `tomaccoctl setup` makes) and the SHA-256 recorded here when it
# was pinned. sdkmanager then installs the pinned platform and build tools and
# the current platform-tools, checking each against Google's index.
# Version 22.0, not 23.0: 23.0's sdkmanager is a wrapper that fetches Google's
# new Android CLI at run time and collects usage data by default.
ARG ANDROID_CMDLINE_TOOLS=commandlinetools-linux-15859902_latest.zip
ARG ANDROID_CMDLINE_TOOLS_SHA1=040d3996a65543d22ec4bf73e4c37aa37a8d4af4
ARG ANDROID_CMDLINE_TOOLS_SHA256=4e4c464f145a7512b57d088ac6c278c03c9eea610886b35a5e0804e74eedf583
ARG ANDROID_PLATFORM=platforms;android-36
ARG ANDROID_BUILD_TOOLS=build-tools;36.0.0
RUN set -eu; \
    curl -fsSL --retry 3 -o /tmp/cmdline-tools.zip "https://dl.google.com/android/repository/${ANDROID_CMDLINE_TOOLS}"; \
    echo "${ANDROID_CMDLINE_TOOLS_SHA1}  /tmp/cmdline-tools.zip" | sha1sum -c -; \
    echo "${ANDROID_CMDLINE_TOOLS_SHA256}  /tmp/cmdline-tools.zip" | sha256sum -c -; \
    mkdir -p /tmp/cmdline-tools "$ANDROID_HOME/cmdline-tools"; \
    unzip -q /tmp/cmdline-tools.zip -d /tmp/cmdline-tools; \
    mv /tmp/cmdline-tools/cmdline-tools "$ANDROID_HOME/cmdline-tools/latest"; \
    rm -rf /tmp/cmdline-tools /tmp/cmdline-tools.zip; \
    yes | sdkmanager --licenses >/dev/null; \
    sdkmanager --install platform-tools "$ANDROID_PLATFORM" "$ANDROID_BUILD_TOOLS" > /tmp/sdkmanager.log 2>&1 \
        || { tr '\r' '\n' < /tmp/sdkmanager.log | tail -n 40; exit 1; }; \
    test -x "$ANDROID_HOME/platform-tools/adb"; \
    test -d "$ANDROID_HOME/$(echo "$ANDROID_PLATFORM" | tr ';' /)"; \
    test -d "$ANDROID_HOME/$(echo "$ANDROID_BUILD_TOOLS" | tr ';' /)"; \
    rm -rf /data/android "$HOME/.android" "$HOME/.java" /tmp/hsperfdata_tomacco /tmp/sdkmanager.log

# The emulator variant: the Android Emulator, one x86_64 system image (the one
# `tomaccoctl setup` uses) and the libraries the emulator loads even without a
# window. Several gigabytes, so off by default. Running it needs --device /dev/kvm.
ARG ANDROID_EMULATOR=0
ARG ANDROID_SYSTEM_IMAGE=system-images;android-36;google_apis;x86_64
USER root
RUN if [ "$ANDROID_EMULATOR" = 1 ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            libpulse0 libgl1 libegl1 libnss3 libxcomposite1 libxcursor1 libxdamage1 libxi6 libxtst6 \
            libxrandr2 libxkbfile1 libasound2t64 libdbus-1-3 libx11-xcb1 libxkbcommon0 libbsd0 libpng16-16t64 \
        && rm -rf /var/lib/apt/lists/*; \
    fi
USER tomacco
# What sdkmanager installed, with versions, for the record.
RUN set -eu; \
    if [ "$ANDROID_EMULATOR" = 1 ]; then \
        sdkmanager --install emulator "$ANDROID_SYSTEM_IMAGE" > /tmp/sdkmanager.log 2>&1 \
            || { tr '\r' '\n' < /tmp/sdkmanager.log | tail -n 40; exit 1; }; \
        test -x "$ANDROID_HOME/emulator/emulator"; \
        test -d "$ANDROID_HOME/$(echo "$ANDROID_SYSTEM_IMAGE" | tr ';' /)"; \
    fi; \
    sdkmanager --list_installed 2>/dev/null | tr '\r' '\n' | grep '|' > "$ANDROID_HOME/installed-packages.txt"; \
    rm -rf /data/android "$HOME/.android" "$HOME/.java" /tmp/hsperfdata_tomacco /tmp/sdkmanager.log

LABEL org.opencontainers.image.title="Tomacco (headless)" \
      org.opencontainers.image.description="tomaccoctl with a JDK, Gradle and the Android SDK, for coding agents, servers and CI" \
      org.opencontainers.image.source="https://github.com/marselnenaj/tomacco" \
      org.opencontainers.image.licenses="GPL-2.0-or-later"

# Jobs, settings, Gradle's caches and the emulators' state. Mount a named
# volume here to keep them; it must be writable by UID 1000.
VOLUME ["/data"]
ENTRYPOINT ["/usr/bin/tini", "--", "tomaccoctl"]
CMD ["--help"]
