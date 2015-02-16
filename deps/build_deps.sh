#!/bin/sh

if [ -z "$VIRTUAL_ENV" ]; then
	echo "Please activate your virtulaenv first"
	exit 1
fi

pip install pytz

ROOTDIR="$(dirname "$(readlink -fn "$0")")"

git submodule init
git submodule update
echo $ROOTDIR

rm -rf $ROOTDIR/build
mkdir -p $ROOTDIR/build/libgit2
cd $ROOTDIR/build/libgit2
cmake $ROOTDIR/libgit2 -DCMAKE_INSTALL_PREFIX=$VIRTUAL_ENV
make
make install


cd $ROOTDIR/pygit2
export CFLAGS="-I$VIRTUAL_ENV/include $CFLAGS"
export LDFLAGS="-L$VIRTUAL_ENV/lib -Wl,-rpath='$VIRTUAL_ENV/lib',--enable-new-dtags $LDFLAGS"
python setup.py build install
