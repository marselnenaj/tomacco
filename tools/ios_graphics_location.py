"""One fixed location for the prepared native graphics bundle and its alias.

The compact name fits the original kernel prelink XML slot. It lives under
the retained Cryptexes tree; a top-level directory of our own under Preboot
disappeared in an observed boot. This declaration alone does not qualify persistence.
"""
GRAPHICS_RELATIVE_PATH = "Cryptexes/ps.bundle"
BUNDLE_PATH = "/private/preboot/" + GRAPHICS_RELATIVE_PATH
PLUGIN_PERSONALITY = "../../../" + BUNDLE_PATH.removeprefix("/").removesuffix(".bundle")
COMPILER_LIBRARY_NAME = "libAppleParavirtCompilerPluginIOGPUFamily.dylib"


def bridge_path_header():
    compiler = BUNDLE_PATH + "/" + COMPILER_LIBRARY_NAME
    alias = "/System/Library/Extensions/" + PLUGIN_PERSONALITY + ".bundle/" + COMPILER_LIBRARY_NAME
    return ("/* Fixed native graphics paths; generated from ios_graphics_location.py. */\n"
            f'#define TOMACCO_GPU_COMPILER_PATH "{compiler}"\n'
            f'#define TOMACCO_GPU_COMPILER_ALIAS_PATH "{alias}"\n').encode()
