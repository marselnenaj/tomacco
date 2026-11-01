cmake_minimum_required(VERSION 3.22)
project(Tomacco VERSION 1.2.1 LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)
# QML icons also require the SVG module and its image-format plugin at runtime.
find_package(Qt6 6.9 REQUIRED COMPONENTS Quick QuickControls2 WebEngineQuick Network Svg)
# Qt's lupdate/lrelease (qt6-tools on Arch). Without them the translations are
# compiled by tools/ts2qm.py, so the translated interface builds everywhere.
find_package(Qt6 QUIET OPTIONAL_COMPONENTS LinguistTools)
# Desktop notifications go over the session bus (org.freedesktop.Notifications).
# Without Qt D-Bus the desktop builds and simply sends none.
find_package(Qt6 QUIET OPTIONAL_COMPONENTS DBus)
# For distribution packages: the package manager updates, so tomaccoctl never asks GitHub.
option(TOMACCO_DISABLE_UPDATES "Build without update checks and installation" OFF)
option(TOMACCO_USE_TS2QM "Compile translations with tools/ts2qm.py even when Qt's lrelease is available" OFF)
find_package(PkgConfig REQUIRED)
# Python runs the documentation packaging below and the tests.
find_package(Python3 REQUIRED COMPONENTS Interpreter)
pkg_check_modules(AV REQUIRED IMPORTED_TARGET libavcodec libavutil libswscale)
find_program(CARGO_EXECUTABLE cargo HINTS "${CMAKE_CURRENT_SOURCE_DIR}/.tooling/cargo/bin" REQUIRED)
set(TOMACCO_RUST_ENV)
if(CARGO_EXECUTABLE MATCHES "/.tooling/cargo/bin/cargo$")
    list(APPEND TOMACCO_RUST_ENV "CARGO_HOME=${CMAKE_CURRENT_SOURCE_DIR}/.tooling/cargo" "RUSTUP_HOME=${CMAKE_CURRENT_SOURCE_DIR}/.tooling/rustup")
endif()
file(GLOB_RECURSE TOMACCO_RUST_SOURCES CONFIGURE_DEPENDS native/*.rs assets/templates/*)
set(TOMACCO_CARGO_TARGET "${CMAKE_CURRENT_BINARY_DIR}/cargo")
# Keep this machine's paths out of the released binaries: panic locations and
# debug information name /tomacco, /tomacco-build and /cargo
# instead of the source, build and Cargo home directories. rustc applies the
# last matching map, so the more specific directories come later.
if(DEFINED ENV{CARGO_HOME})
    set(TOMACCO_CARGO_HOME "$ENV{CARGO_HOME}")
else()
    set(TOMACCO_CARGO_HOME "$ENV{HOME}/.cargo")
endif()
if(CARGO_EXECUTABLE MATCHES "/.tooling/cargo/bin/cargo$")
    set(TOMACCO_CARGO_HOME "${CMAKE_CURRENT_SOURCE_DIR}/.tooling/cargo")
endif()
set(TOMACCO_RUST_BUILD_ENV ${TOMACCO_RUST_ENV})
set(TOMACCO_PATH_MAPS "${CMAKE_CURRENT_SOURCE_DIR}=/tomacco" "${CMAKE_CURRENT_BINARY_DIR}=/tomacco-build" "${TOMACCO_CARGO_HOME}=/cargo")
if("${TOMACCO_PATH_MAPS}" MATCHES " ")
    message(WARNING "A source, build or Cargo directory contains a space; tomaccoctl keeps its build paths")
else()
    list(TRANSFORM TOMACCO_PATH_MAPS PREPEND "--remap-path-prefix=" OUTPUT_VARIABLE TOMACCO_RUST_REMAPS)
    list(JOIN TOMACCO_RUST_REMAPS " " TOMACCO_RUST_REMAPS)
    string(STRIP "$ENV{RUSTFLAGS} ${TOMACCO_RUST_REMAPS}" TOMACCO_RUSTFLAGS)
    list(APPEND TOMACCO_RUST_BUILD_ENV "RUSTFLAGS=${TOMACCO_RUSTFLAGS}")
endif()
if(TOMACCO_DISABLE_UPDATES)
    list(APPEND TOMACCO_RUST_BUILD_ENV "TOMACCO_UPDATES_BUILD_OFF=off")
endif()
add_custom_command(OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/tomaccoctl"
    COMMAND ${CMAKE_COMMAND} -E env ${TOMACCO_RUST_BUILD_ENV} "CARGO_TARGET_DIR=${TOMACCO_CARGO_TARGET}" ${CARGO_EXECUTABLE} build --release --locked --manifest-path "${CMAKE_CURRENT_SOURCE_DIR}/Cargo.toml"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different "${TOMACCO_CARGO_TARGET}/release/tomaccoctl" "${CMAKE_CURRENT_BINARY_DIR}/tomaccoctl"
    DEPENDS ${TOMACCO_RUST_SOURCES} Cargo.toml Cargo.lock build.rs native/protocol/idb.proto assets/models.json assets/device-profile.schema.json assets/operations.json assets/errors.json assets/layout-audit.js
    WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}" VERBATIM)
add_custom_target(tomacco-core ALL DEPENDS "${CMAKE_CURRENT_BINARY_DIR}/tomaccoctl")
qt_add_executable(tomacco src/main.cpp src/backend.cpp src/backend.h src/control.cpp src/control.h src/nativeview.cpp src/nativeview.h)
add_dependencies(tomacco tomacco-core)
get_target_property(qt_core_type Qt6::Core TYPE)
if(qt_core_type STREQUAL "SHARED_LIBRARY")
    # Dynamic Qt discovers the resource imports at runtime. The optional
    # Omarchy plugin is loaded by Quickshell, never linked into this desktop.
    set_target_properties(tomacco PROPERTIES QT_QML_MODULE_NO_IMPORT_SCAN TRUE)
endif()
# Running from the build tree finds tomaccoctl next to the binary and the legal
# documents in its parent directory, so the desktop compiles in no path of this
# machine unless asked to (Backend::documentPath's last fallback).
option(TOMACCO_EMBED_SOURCE_DIR "Compile the source directory into the desktop as a last fallback for the legal documents" OFF)
if(TOMACCO_EMBED_SOURCE_DIR)
    target_compile_definitions(tomacco PRIVATE TOMACCO_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}")
endif()
target_compile_definitions(tomacco PRIVATE TOMACCO_VERSION="${PROJECT_VERSION}")
set(TOMACCO_QML_RESOURCES
    qml/StudioScrollView.qml qml/StudioScrollBar.qml qml/AppDropArea.qml qml/AppFiles.js qml/LogFilter.js qml/Fuzzy.js qml/DropOverlay.qml
    qml/NativeDeviceFrame.qml qml/DeviceWindow.qml qml/NativeProfiles.js qml/AndroidDevices.qml qml/IosResearch.qml qml/IosGuestView.qml qml/ScreenRecorder.qml qml/DeviceGrid.qml qml/DeviceLogPane.qml qml/StageLogPane.qml
    qml/qmldir qml/Theme.qml qml/Theme.js qml/ActivityPanel.qml qml/StudioHeading.qml qml/StudioBand.qml qml/BrandMark.qml qml/DeviceHero.qml qml/StudioStatus.qml qml/StudioSection.qml qml/StudioPanel.qml qml/StudioEmptyState.qml qml/StudioSegmented.qml qml/StudioSpinBox.qml qml/StudioCodeView.qml qml/OperationTitles.js qml/ColorNames.js qml/StudioIcon.qml qml/StudioTextField.qml qml/StudioComboBox.qml qml/StudioCheckBox.qml qml/StudioDialog.qml qml/CommandPalette.qml qml/Main.qml qml/StudioButton.qml qml/Device.qml qml/Workspace.qml qml/NativeWorkspace.qml qml/DebugWorkspace.qml qml/TestWorkspace.qml qml/ProfileWorkspace.qml qml/FileWorkspace.qml qml/ReleaseWorkspace.qml qml/PreviewAutomation.qml qml/PreviewProtocol.qml qml/SystemStatus.qml qml/UpdateModel.qml qml/UpdateSection.qml qml/UpdateNotice.qml qml/UpdateBanner.qml qml/OnboardingSheet.qml
    assets/models.json assets/demo.html assets/page-errors.js assets/icon.svg assets/logo.svg assets/fonts/archivo-var.ttf assets/fonts/jetbrains-mono-var.ttf)
qt_add_resources(tomacco resources PREFIX / FILES ${TOMACCO_QML_RESOURCES})
# Interface translations. English is the source language: its file only
# carries plural forms. Each other language has one file with every text.
set(TOMACCO_TS_FILE i18n/tomacco_de.ts i18n/tomacco_fr.ts i18n/tomacco_es.ts i18n/tomacco_it.ts)
set(TOMACCO_PLURALS_TS_FILE i18n/tomacco_en.ts)
if(TARGET Qt6::lrelease AND NOT TOMACCO_USE_TS2QM)
    # lupdate reads the target's C++ and QML/JS sources; the other resources hold no UI text.
    set_property(TARGET tomacco PROPERTY QT_EXCLUDE_SOURCES_FROM_TRANSLATION "assets/*")
    # Refresh the .ts files after changing texts: cmake --build <dir> --target update_translations
    qt_add_translations(tomacco IMMEDIATE_CALL
        TS_FILES ${TOMACCO_TS_FILE}
        PLURALS_TS_FILE ${TOMACCO_PLURALS_TS_FILE}
        LUPDATE_OPTIONS -locations none
        QM_FILES_OUTPUT_VARIABLE TOMACCO_QM_FILES)
else()
    set(TOMACCO_QM_FILES)
    foreach(ts ${TOMACCO_TS_FILE} ${TOMACCO_PLURALS_TS_FILE})
        get_filename_component(name "${ts}" NAME_WE)
        set(qm "${CMAKE_CURRENT_BINARY_DIR}/i18n/${name}.qm")
        add_custom_command(OUTPUT "${qm}"
            COMMAND ${CMAKE_COMMAND} -E make_directory "${CMAKE_CURRENT_BINARY_DIR}/i18n"
            COMMAND ${Python3_EXECUTABLE} "${CMAKE_CURRENT_SOURCE_DIR}/tools/ts2qm.py" "${CMAKE_CURRENT_SOURCE_DIR}/${ts}" "${qm}"
            DEPENDS "${ts}" tools/ts2qm.py VERBATIM)
        list(APPEND TOMACCO_QM_FILES "${qm}")
    endforeach()
endif()
foreach(qm ${TOMACCO_QM_FILES})
    get_filename_component(name "${qm}" NAME)
    set_source_files_properties("${qm}" PROPERTIES QT_RESOURCE_ALIAS "${name}" GENERATED TRUE)
endforeach()
qt_add_resources(tomacco translations PREFIX /i18n FILES ${TOMACCO_QM_FILES})
target_link_libraries(tomacco PRIVATE Qt6::Quick Qt6::QuickControls2 Qt6::WebEngineQuick Qt6::Network PkgConfig::AV)
if(TARGET Qt6::DBus)
    target_link_libraries(tomacco PRIVATE Qt6::DBus)
    target_compile_definitions(tomacco PRIVATE TOMACCO_HAVE_DBUS)
endif()
target_compile_options(tomacco PRIVATE -Wall -Wextra -Wpedantic
    "-ffile-prefix-map=${CMAKE_CURRENT_SOURCE_DIR}=/tomacco" "-ffile-prefix-map=${CMAKE_CURRENT_BINARY_DIR}=/tomacco-build")
install(TARGETS tomacco RUNTIME DESTINATION bin)
install(PROGRAMS "${CMAKE_CURRENT_BINARY_DIR}/tomaccoctl" DESTINATION bin)
install(FILES docs/tomaccoctl.1 DESTINATION share/man/man1)
install(FILES assets/models.json assets/device-profile.schema.json assets/operations.json DESTINATION lib/tomacco/assets)
# The package's own installer: run from the unpacked tarball, it installs for
# one user what it finds beside it, and share/tomacco/VERSION tells it that it is
# not in a source tree.
install(PROGRAMS install.py DESTINATION .)
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/VERSION" "${PROJECT_VERSION}\n")
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/VERSION" DESTINATION share/tomacco)
install(DIRECTORY integrations/omarchy DESTINATION share/tomacco)
# The recipes' sources and patches with their README and licenses; the
# research notes beside them stay in the source tree.
install(DIRECTORY integrations/ios-emulator DESTINATION share/tomacco/integrations
    PATTERN "*.md" EXCLUDE)
install(PROGRAMS tools/build_ios_emulator.py tools/prepare_ios_emulator.py tools/build_ios_native_example.py tools/build_ios_research_bootstrap.py tools/build_ios_storage_probe.py tools/prepare_ios_research_inputs.py tools/extract_ios_stackshots.py tools/extract_ios_graphics.py tools/extract_ios_application.py tools/extract_ios_platform.py DESTINATION share/tomacco/tools)
install(PROGRAMS tools/prepare_ios_research_runtime.py tools/prepare_ios_exclave.py tools/prepare_ios_pmp.py DESTINATION share/tomacco/tools)
install(PROGRAMS tools/prepare_ios_userland.py tools/build_ios_gpu_plugin.py tools/build_ios_linker.py DESTINATION share/tomacco/tools)
install(PROGRAMS tools/prepare_ios_cryptex.py DESTINATION share/tomacco/tools)
install(PROGRAMS tools/prepare_ios_research_manifest.py tools/check_ios_inputs.py tools/prepare_ios_boot_inputs.py DESTINATION share/tomacco/tools)
install(PROGRAMS tools/build_ios_guest_tools.py tools/prepare_ios_guest_tools.py tools/rebase_ios_guest_tools.py tools/build_ios_guest_control.py tools/ios_application_bundle.py tools/build_ios_sample_app.py tools/install_apple_dev.py DESTINATION share/tomacco/tools)
install(PROGRAMS tools/collect_ios_crash_reports.py tools/ios_persistence_probe.py DESTINATION share/tomacco/tools)
install(PROGRAMS tools/collect_ios_service_artifacts.py DESTINATION share/tomacco/tools)
install(PROGRAMS tools/build_source_release.py tools/build_release_notes.py tools/make_release.py tools/build_portable.py tools/build_reference.py tools/build_container.py tools/check_dependencies.py tools/build_sbom.py DESTINATION share/tomacco/tools)
install(FILES tools/ios_guest_tools.py tools/ios_guest_control.py tools/build_provenance.py DESTINATION share/tomacco/tools)
install(FILES tools/ios_host_pressure.py tools/ios_hot_install.py DESTINATION share/tomacco/tools)
install(PROGRAMS tools/ios_debug_bridge.py tools/ios_virtual_record.py tools/ios_device_record.py tools/ios_ipa_inspect.py DESTINATION share/tomacco/tools)
install(FILES tools/ios_apfs_layout.py tools/ios_paravirt_kernel.py tools/ios_paravirt_graphics.py tools/ios_graphics_location.py DESTINATION share/tomacco/tools)
install(PROGRAMS tools/build_ios_paravirt.py tools/ios_paravirt_build.py tools/prepare_ios_paravirt.py tools/prepare_ios_cpu_topology.py tools/prepare_ios_fileport_compat.py tools/prepare_ios_workloop_compat.py tools/prepare_ios_adapter_pin.py tools/prepare_ios_paravirt_restore.py tools/run_ios_paravirt.py tools/ios_paravirt_storage.py tools/ios_disk_overlay.py tools/ios_run_verdict.py tools/ios_start_timing.py tools/ios_virtual_iphone.py tools/ios_virtual_profile.py tools/ios_virtual_doctor.py tools/ios_lifecycle.py tools/ios_measure.py tools/engine_pac_bench.py tools/ios_tcg_profile.py tools/engine_branch_bench.py tools/ios_paravirt_usb.py tools/ios_paravirt_usbmuxd.py tools/ios_paravirt_firmware.py tools/ios_paravirt_restore.py DESTINATION share/tomacco/tools)
install(FILES tools/ios_paravirt_mux.py tools/ios_paravirt_duplex.py tools/ios_paravirt_asr.py tools/ios_paravirt_manifest.py tools/ios_paravirt_seal.py tools/ios_paravirt_userland.py tools/ios_paravirt_pcc.py DESTINATION share/tomacco/tools)
install(DIRECTORY examples/ios-restore-native DESTINATION share/tomacco/examples)
install(FILES assets/icon.svg DESTINATION share/icons/hicolor/scalable/apps RENAME io.github.marselnenaj.tomacco.svg)
install(FILES assets/io.github.marselnenaj.tomacco.desktop DESTINATION share/applications)
install(FILES assets/io.github.marselnenaj.tomacco.metainfo.xml DESTINATION share/metainfo)
# The user guides and legal documents (install.py DOCUMENTS), with links
# adjusted to the flat directory, the GPL-3.0 text and SOURCE.md naming this
# version and commit; the developer notes stay in the source tree. The
# About dialog opens LICENSE, THIRD_PARTY_NOTICES.md and legal.md from here.
# Rewritten on every build so SOURCE.md names the current commit. The engine
# recipes' README gets the same link treatment.
set(TOMACCO_PACKAGE_DIR "${CMAKE_CURRENT_BINARY_DIR}/package")
add_custom_target(tomacco-docs ALL
    COMMAND ${Python3_EXECUTABLE} "${CMAKE_CURRENT_SOURCE_DIR}/install.py" --package-docs "${TOMACCO_PACKAGE_DIR}"
    WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}" VERBATIM)
install(DIRECTORY "${TOMACCO_PACKAGE_DIR}/doc/" DESTINATION share/doc/tomacco)
install(FILES "${TOMACCO_PACKAGE_DIR}/integrations/ios-emulator/README.md" DESTINATION share/tomacco/integrations/ios-emulator)
set(CPACK_GENERATOR TGZ)
set(CPACK_PACKAGE_NAME tomacco)
set(CPACK_PACKAGE_VERSION ${PROJECT_VERSION})
set(CPACK_RESOURCE_FILE_LICENSE "${CMAKE_CURRENT_SOURCE_DIR}/LICENSE")
set(CPACK_PACKAGE_FILE_NAME "tomacco-${PROJECT_VERSION}-${CMAKE_SYSTEM_NAME}-${CMAKE_SYSTEM_PROCESSOR}")
set(CPACK_PACKAGE_CHECKSUM SHA256)
# The same build gives the same package: entries with UID and GID 0 (CMake 4.3
# and newer; older versions ignore these; the owner's name is still recorded),
# and with SOURCE_DATE_EPOCH set, as make_release.py sets it, CPack's time
# stamps and rcc's are that time.
set(CPACK_ARCHIVE_UID 0)
set(CPACK_ARCHIVE_GID 0)
include(CPack)
include(CTest)
if(BUILD_TESTING)
    find_package(Qt6 REQUIRED COMPONENTS Test)
    qt_add_executable(test-desktop-ui tests/desktop_ui_test.cpp src/backend.cpp src/backend.h src/control.cpp src/control.h src/nativeview.cpp src/nativeview.h)
    set_target_properties(test-desktop-ui PROPERTIES QT_QML_MODULE_NO_IMPORT_SCAN TRUE)
    add_dependencies(test-desktop-ui tomacco-core)
    qt_add_resources(test-desktop-ui ui-resources PREFIX / FILES ${TOMACCO_QML_RESOURCES})
    qt_add_resources(test-desktop-ui ui-translations PREFIX /i18n FILES ${TOMACCO_QM_FILES})
    target_include_directories(test-desktop-ui PRIVATE src)
    target_compile_definitions(test-desktop-ui PRIVATE TOMACCO_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}")
    target_link_libraries(test-desktop-ui PRIVATE Qt6::Test Qt6::Quick Qt6::QuickControls2 Qt6::WebEngineQuick Qt6::Network PkgConfig::AV)
    if(TARGET Qt6::DBus)
        target_link_libraries(test-desktop-ui PRIVATE Qt6::DBus)
        target_compile_definitions(test-desktop-ui PRIVATE TOMACCO_HAVE_DBUS)
    endif()
    add_test(NAME desktop-ui COMMAND test-desktop-ui)
    set_tests_properties(desktop-ui PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software;QT_QUICK_CONTROLS_STYLE=Basic;QT_QPA_PLATFORMTHEME=generic;QT_FORCE_STDERR_LOGGING=1;TOMACCO_UPDATES=off")
    qt_add_executable(test-backend tests/backend_test.cpp src/backend.cpp src/backend.h)
    qt_add_resources(test-backend fixtures PREFIX / FILES assets/models.json)
    target_include_directories(test-backend PRIVATE src)
    target_link_libraries(test-backend PRIVATE Qt6::Test Qt6::Gui Qt6::Network)
    if(TARGET Qt6::DBus)
        target_link_libraries(test-backend PRIVATE Qt6::DBus)
        target_compile_definitions(test-backend PRIVATE TOMACCO_HAVE_DBUS)
    endif()
    add_test(NAME backend COMMAND test-backend)
    qt_add_executable(test-nativeview tests/nativeview_test.cpp src/nativeview.cpp src/nativeview.h)
    set_target_properties(test-nativeview PROPERTIES QT_QML_MODULE_NO_IMPORT_SCAN TRUE)
    target_include_directories(test-nativeview PRIVATE src)
    target_compile_definitions(test-nativeview PRIVATE TOMACCO_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}")
    target_link_libraries(test-nativeview PRIVATE Qt6::Test Qt6::Quick Qt6::Network PkgConfig::AV)
    add_test(NAME native-display COMMAND test-nativeview)
    set_tests_properties(native-display PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software")
    add_test(NAME control-plane COMMAND ${Python3_EXECUTABLE} -m unittest discover -s tests/python -v)
    set_tests_properties(control-plane PROPERTIES WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
        ENVIRONMENT "TOMACCOCTL=${CMAKE_CURRENT_BINARY_DIR}/tomaccoctl;TOMACCO_UPDATES=off")
    add_test(NAME rust-core COMMAND ${CMAKE_COMMAND} -E env ${TOMACCO_RUST_ENV} "CARGO_TARGET_DIR=${TOMACCO_CARGO_TARGET}" ${CARGO_EXECUTABLE} test --offline --locked --manifest-path "${CMAKE_CURRENT_SOURCE_DIR}/Cargo.toml")
endif()
