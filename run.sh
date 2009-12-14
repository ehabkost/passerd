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

add_pypath "$mydir/third-party/twitty-twister"
add_pypath "$mydir"
export PYTHONPATH

mkdir -p "$HOME/.passerd"
"$mydir/bin/passerd" "$HOME/.passerd/data.sqlite"
