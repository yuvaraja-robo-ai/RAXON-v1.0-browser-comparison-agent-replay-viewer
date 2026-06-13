#!/usr/bin/env bash
# Assemble the S9 demo video: title card + live HF run + replay-UI walkthrough.
# Inputs come from scripts/record_demo.py (demo/raw/{live,ui}/*.webm).
# Output: demo/RAXON_browser_agent_demo.mp4 — H.264 720p30 yuv420p +faststart.
set -euo pipefail
cd "$(dirname "$0")"

FONT=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
LIVE=$(ls raw/live/*.webm)
UI=$(ls raw/ui/*.webm)
ENC=(-r 30 -c:v libx264 -preset veryfast -crf 22 -pix_fmt yuv420p -an)

ffmpeg -y -v error -f lavfi -i color=c=0x0d1322:s=1280x720:d=4 -vf "
drawtext=fontfile=$FONT:text='RAXON — Browser Comparison Agent + Replay Viewer':fontcolor=white:fontsize=40:x=(w-text_w)/2:y=290,
drawtext=fontfile=$FONT:text='Compare the top 3 most-liked Hugging Face text-generation models':fontcolor=0xffd24a:fontsize=24:x=(w-text_w)/2:y=370,
drawtext=fontfile=$FONT:text='deterministic capture · 5 visible actions · \$0 LLM cost · orchestrator unmodified':fontcolor=0x9fb0d0:fontsize=20:x=(w-text_w)/2:y=420" \
  "${ENC[@]}" _title.mp4
ffmpeg -y -v error -i "$LIVE" -vf scale=1280:720 "${ENC[@]}" _live.mp4
ffmpeg -y -v error -i "$UI"   -vf scale=1280:720 "${ENC[@]}" _ui.mp4

printf "file '_title.mp4'\nfile '_live.mp4'\nfile '_ui.mp4'\n" > _concat.txt
ffmpeg -y -v error -f concat -safe 0 -i _concat.txt -c copy -movflags +faststart \
  RAXON_browser_agent_demo.mp4
rm -f _title.mp4 _live.mp4 _ui.mp4 _concat.txt
ffprobe -v error -show_entries format=duration,size -of default=nw=1 RAXON_browser_agent_demo.mp4
