#!/usr/bin/env bash
# Export an index of every single-turn Council seat prompt in Agora's prompt
# archive: metadata only (decision, seat, time, digest, sizes, recorded token
# usage), never prompt text. Single-turn means one user message: a seat's
# opening request for a phase (the recusal question, or Round 1), before any
# tool call. Runs on the Agora host as the read-only council role. Output
# goes to inputs/ (gitignored).
#
# Usage: scripts/export_archive_index.sh
set -euo pipefail
out="$(dirname "$0")/../inputs/archive-index.tsv"
docker exec -i agora-postgres-1 psql -U agora_council -d agora -At -F$'\t' -v ON_ERROR_STOP=1 \
  <<'SQL' > "$out"
SELECT d.id, pa.role, d.agenda_item_id, d.meeting_id,
       to_char(pa.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
       pa.digest,
       jsonb_array_length(pa.prompt->'messages'),
       jsonb_array_length(pa.prompt->'messages'->0->'content'),
       coalesce(pa.input_tokens, -1), coalesce(pa.cache_read_tokens, -1),
       coalesce(pa.cache_creation_tokens, -1), coalesce(pa.output_tokens, -1)
FROM prompt_archive pa
JOIN council_decisions d ON pa.context_id = d.agenda_item_id
WHERE d.id LIKE 'GOV-%'
  AND pa.role::text IN ('artist', 'philosopher', 'lawyer', 'engineer')
  AND jsonb_array_length(pa.prompt->'messages') = 1
ORDER BY d.id, pa.created_at;
SQL
printf 'decision\trole\tagenda_item\tmeeting\tcreated_at\tdigest\tn_messages\tn_blocks\tinput_tokens\tcache_read\tcache_creation\toutput_tokens\n' \
  | cat - "$out" > "$out.tmp" && mv "$out.tmp" "$out"
wc -l "$out"
