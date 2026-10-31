The idb protocol subset follows `facebook/idb/proto/idb.proto`.
Copyright (c) Meta Platforms, Inc. and affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
IN THE SOFTWARE.

The scrcpy adapter interoperates with the version 4.1 protocol published by
Genymobile: https://github.com/Genymobile/scrcpy/tree/v4.1. The separate server
is installed by scrcpy and is not redistributed in Tomacco's binary.

The physical-iPhone adapter interoperates with Appium WebDriverAgent's HTTP
and MJPEG protocols. `ios.wda_prepare` downloads the separately licensed
official v16.13.6 device package on request and verifies its SHA-256. It is not
embedded in Tomacco. WebDriverAgent is copyright Facebook, Inc. and is
licensed under BSD-3-Clause: https://github.com/appium/WebDriverAgent/blob/v16.13.6/LICENSE

The separate go-ios tool is copyright Daniel Paulus and contributors, MIT:
https://github.com/danielpaulus/go-ios/blob/v1.3.2/LICENSE
iproxy is provided by libusbmuxd. xtool, Swift and Apple's SDK remain separate
tools with their own licenses; Tomacco does not distribute Apple's SDK.

The optional Apple ARM research engines are separate QEMU derivatives:
jprx/qemu-sptm for SPTM/TXM restore-shell execution, and ChefKiss Inferno for
the earlier T8030 kernel probe. `integrations/ios-emulator/` records source
pins, notices, the Inferno-only patch and GPL/AGPL texts. Linux preparation
uses darwin-vm (MIT), linux-apfs-rw (GPL-2.0), ldid and BusyBox as separately
downloaded tools. No external engine, guest utility or Apple firmware binary
is embedded in Tomacco. `docs/research-engines.md` explains the scope.
