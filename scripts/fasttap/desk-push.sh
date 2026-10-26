#!/bin/sh
# Desk side: copy this branch to the phone kit dir and build there.
# Does not stop, restart or install anything. Usage: desk-push.sh [host]
set -eu
H=${1:-fp5-pmos}
R=$(cd "$(dirname "$0")/../.." && pwd)
K=/home/user/fp5-qtee-keep/fasttap-20261026
rev=$(git -C "$R" rev-parse --short HEAD)
ssh "$H" "mkdir -p $K/src $K/build $K/app && touch $K/PROGRESS.md"
tar -C "$R" -cf - session app scripts/fasttap docs/fasttap-test-plan.md |
	ssh "$H" "rm -rf $K/src.new && mkdir $K/src.new && tar -C $K/src.new -xf - && rm -rf $K/src && mv $K/src.new $K/src"
ssh "$H" "set -e; cd $K/src/session && make clean >/dev/null 2>&1 || true; make fp5-qtee-session 2>&1 | grep -v builtin-declaration | grep -E 'error|warning' || true
  cp $K/src/session/fp5-qtee-session $K/build/fp5-qtee-session.new && mv -f $K/build/fp5-qtee-session.new $K/build/fp5-qtee-session
  rm -rf $K/app.new && mkdir $K/app.new && cp $K/src/app/*.py $K/src/app/*.json $K/app.new/ && rm -rf $K/app && mv $K/app.new $K/app
  cp $K/src/scripts/fasttap/*.sh $K/ && chmod +x $K/*.sh
  cp $K/src/docs/fasttap-test-plan.md $K/
  echo \"\$(date +%FT%T%z) pushed $rev, built \$(sha256sum $K/build/fp5-qtee-session | cut -c1-16)\" | tee -a $K/PROGRESS.md"
