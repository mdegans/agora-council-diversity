#!/usr/bin/env bash
# Compare each seat's recorded Round 1 vote (Opus 4.6, from the signed record)
# with a replay's first action. Prints votes and tool names only, no text.
set -uo pipefail
cd "$(dirname "$0")/../inputs"
model="${MODEL:-Qwen3.8-27B-UD-Q8_K_XL.gguf}"
printf '%-14s %-12s %-8s %-8s %-14s %s\n' item seat opus replay replay_tool note
for rec in records/GOV-*.r1.json; do
  gov=$(basename "$rec" .r1.json)
  for role in artist philosopher lawyer engineer; do
    opus=$(jq -r --arg r "$role" '.data.rounds[0].responses[] | select(.role==$r) | .vote // "none"' "$rec")
    resp="$gov/out/$role.$model.response.json"
    if [ ! -f "$resp" ]; then printf '%-14s %-12s %-8s %-8s\n' "$gov" "$role" "$opus" pending; continue; fi
    tool=$(jq -r '[.content[]? | select(.type=="tool_use") | .name] | join(",")' "$resp")
    vote=$(jq -r '[.content[]? | select(.type=="tool_use") | .input.vote // empty] | first // "-"' "$resp")
    note=""; [ "$vote" != "-" ] && [ "$vote" != "$opus" ] && note="DIFFERENT"
    [ "$vote" = "-" ] && note="no vote in first action"
    printf '%-14s %-12s %-8s %-8s %-14s %s\n' "$gov" "$role" "$opus" "$vote" "$tool" "$note"
  done
done
