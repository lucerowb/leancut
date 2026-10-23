# Test fixture licences

## gopro13_ride_gpmd.mov

The first 3 s of the GoPro metadata track (`gpmd`, GPMF: GPS9, ACCL, GYRO, ...) of chapter `GL010346.LRV` from
"ride-recap sample ride — 2026-07-10" by Ian Macomber, a real bike ride shot on a GoPro HERO13 Black (firmware
H24.01.02.10.00).

- Source: https://huggingface.co/datasets/iandmacomber/ride-recap-sample-2026-07-10
- Licence: Creative Commons Attribution 4.0 International (CC BY 4.0), https://creativecommons.org/licenses/by/4.0/
- Changes: only the metadata track is kept (no video or audio), cut to its first three packets and remuxed
  unchanged (`ffmpeg -map 0:3 -c copy -frames:d 3 -map_metadata -1 -tag:d gpmd`). The GPMF bytes are not modified.
