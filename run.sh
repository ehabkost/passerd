#!/bin/bash

mydir="$(dirname "$0")"

add_pypath()
{
	if [ -n "$PYTHONPATH" ];then
		PYTHONPATH="$1:$PYTHONPATH"
	else
		PYTHONPATH="$1"
	fi
}

if [ ! -f "$mydir/third-party/twitty-twister/twittytwister/twitter.py" ];then
	echo "You need to update the twitty-twister submodule. Run:" >&2
	echo "    git submodule init"
	echo "    git submodule update"
	exit 1
fi
add_pypath "$mydir/third-party/twitty-twister"
add_pypath "$mydir"
export PYTHONPATH

mkdir -p "$HOME/.passerd"
"$mydir/bin/passerd" "$HOME/.passerd/data.sqlite" "$@"
