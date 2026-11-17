#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# UNTESTED. Installs the GXF/SPRR test kernel as an extra GRUB entry "Linux GXF test",
# for Arch Linux ARM style systems (mkinitcpio, GRUB). The default entry stays your
# normal kernel; pick the test one in the boot menu. Run as root after building the
# patched tree.
#
#   KERNEL_SRC   the built, patched kernel tree (default: ./linux)
#   HOST_KERNEL  your normal kernel's package name, used to find its GRUB entry
#                (for example linux-asahi)
#
# Undo: rm -r /boot/gxf /etc/grub.d/45_gxf "/usr/lib/modules/$RELEASE" && grub-mkconfig -o /boot/grub/grub.cfg
set -eu
: "${HOST_KERNEL:?set HOST_KERNEL to your normal kernel package name, e.g. linux-asahi}"
cd "${KERNEL_SRC:-linux}"
RELEASE=$(make -s kernelrelease)
make -s modules_install
mkdir -p /boot/gxf
cp arch/arm64/boot/Image /boot/gxf/Image
mkinitcpio -k "$RELEASE" -g /boot/gxf/initramfs.img
# The first top-level menuentry of the current config, pointed at the test kernel.
body=$(awk '/^menuentry /{f=1} f{print} f&&/^}/{exit}' /boot/grub/grub.cfg \
  | sed -e "s|^menuentry '[^']*'|menuentry 'Linux GXF test ($RELEASE)'|" \
        -e "s|/vmlinuz-$HOST_KERNEL|/gxf/Image|" \
        -e "s|/initramfs-$HOST_KERNEL.img|/gxf/initramfs.img|" \
        -e '/^[[:space:]]*linux[[:space:]]/ s|$| kvm-arm.apple_gxf=1|')
case "$body" in *"/gxf/Image"*"apple_gxf=1"*"/gxf/initramfs.img"*) ;; *) echo "Could not derive the menu entry" >&2; exit 1;; esac
{ printf '#!/bin/sh\ncat <<'\''GXF'\''\n'; printf '%s\n' "$body"; printf 'GXF\n'; } > /etc/grub.d/45_gxf
chmod 755 /etc/grub.d/45_gxf
grub-mkconfig -o /boot/grub/grub.cfg
grep -n "GXF test" /boot/grub/grub.cfg
