#!/bin/sh


ROOTDIR="$(dirname "$(readlink -fn "$0")")"

echo $ROOTDIR

rm -rf $ROOTDIR/build
mkdir -p $ROOTDIR/build/libgit2
cd $ROOTDIR/build/libgit2
cmake $ROOTDIR/libgit2 -DCMAKE_INSTALL_PREFIX=$ROOTDIR/prefix
make
make install
