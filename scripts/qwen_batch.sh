#!/usr/bin/env bash
# Informal run: every exported Round 1 request, once, on a local blallama model.
# Skips requests that already have a successful manifest. Waits out 529 "busy".
set -uo pipefail
cd "$(dirname "$0")/.."
model="${MODEL:-Qwen3.8-27B-UD-Q8_K_XL.gguf}"
url="${API_URL:-http://192.168.0.123:11435/v1/messages}"
for gdir in inputs/GOV-*; do
  for role in artist philosopher lawyer engineer; do
    req="$gdir/$role.json"; [ -f "$req" ] || continue
    man="$gdir/out/$role.$model.manifest.json"
    if [ -f "$man" ] && [ "$(jq -r .http_status "$man")" = 200 ]; then continue; fi
    while :; do
      line=$(uv run -q scripts/send_once.py "$req" "$gdir/out" --model "$model" --api-url "$url")
      status=$(jq -r .http_status <<<"$line")
      if [ "$status" = 529 ]; then echo "$(date -u +%TZ) busy, waiting: ${gdir#inputs/} $role"; sleep 60; continue; fi
      echo "$(date -u +%TZ) ${gdir#inputs/} $role $(jq -c '{http_status,stop_reason,tools_called,content_types,usage:{in:.usage.input_tokens,out:.usage.output_tokens},elapsed_s}' <<<"$line")"
      break
    done
  done
done
echo "batch done $(date -u +%FT%TZ)"
