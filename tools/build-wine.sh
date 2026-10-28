#!/usr/bin/env bash
set -euo pipefail

# CC/CXX select the native compiler and, on Linux, the glibc compatibility sysroot.
# PATH must contain the pinned LLVM-MinGW target-prefixed tools before other tools.
source_dir=${1:?Wine source directory}
build_dir=${2:?Build directory}
install_dir=${3:?Install directory}
architectures=${4:?Comma-separated Wine PE architectures}

mkdir -p "$build_dir"
cd "$build_dir"
options=(
    --prefix="$install_dir" --enable-archs="$architectures" --with-mingw=llvm-mingw
    --disable-tests --disable-win16
    # Built despite --without-udev/--without-sdl, and enumerates host HID devices through IOKit on macOS.
    --disable-winebus.sys
    --without-x --without-wayland --without-opengl --without-vulkan
    --without-alsa --without-coreaudio --without-oss --without-pulse
    --without-capi --without-cups --without-dbus --without-ffmpeg
    --without-fontconfig --without-freetype --without-gettext --without-gettextpo
    --without-gphoto --without-gnutls --without-gssapi --without-gstreamer
    --without-hwloc --without-krb5 --without-netapi --without-opencl
    --without-pcap --without-pcsclite --without-sane --without-sdl
    --without-udev --without-usb --without-v4l2 --without-va
)
if [[ $(uname -s) == Darwin ]]; then
    options+=(--disable-winemac.drv)
fi
export CFLAGS=-O2 CXXFLAGS=-O2 CROSSCFLAGS=-O2
export PKG_CONFIG_LIBDIR=/nonexistent
unset PKG_CONFIG_PATH
"$source_dir/configure" "${options[@]}" > configure.log 2>&1
make -j"${JOBS:-4}" > build.log 2>&1
make -j"${JOBS:-4}" install-lib > install.log 2>&1
