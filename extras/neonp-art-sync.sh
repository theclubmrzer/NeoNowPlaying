#!/bin/sh
# Copies cover art from the host /tmp (e.g. plasma-browser-integration_artwork_*.jpg) into a
# directory the StreamController Flatpak can see. Requires: playerctl.
DEST="$HOME/.var/app/com.core447.StreamController/data/neonp-art"
mkdir -p "$DEST"
while true; do
  playerctl --all-players --follow metadata --format '{{mpris:artUrl}}' 2>/dev/null | while read -r url; do
    case "$url" in
      file:///tmp/*)
        f="${url#file://}"
        [ -f "$f" ] && cp -f "$f" "$DEST/$(basename "$f")"
        find "$DEST" -type f -mmin +240 -delete 2>/dev/null
        ;;
    esac
  done
  sleep 3
done
