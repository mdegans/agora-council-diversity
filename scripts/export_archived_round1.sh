#!/usr/bin/env bash
# Export each seat's Round 1 request for one Council decision from Agora's
# prompt archive (the first prompt archived for that seat on that item).
# Runs on the Agora host as the read-only council role. Output goes to
# inputs/, which is gitignored: inputs are published as Governance Log
# attachments, never committed here.
#
# Usage: scripts/export_archived_round1.sh GOV-2026-0012
set -euo pipefail
gov="${1:?usage: $0 GOV-YYYY-NNNN}"
[[ "$gov" =~ ^GOV-[0-9]{4}-[0-9]{4}$ ]] || { echo "not a GOV id: $gov" >&2; exit 2; }
out="$(dirname "$0")/../inputs/$gov"
mkdir -p "$out"
for role in artist philosopher lawyer engineer; do
  docker exec -i agora-postgres-1 psql -U agora_council -d agora -At -v ON_ERROR_STOP=1 \
    -v gov="$gov" -v role="$role" <<'SQL' > "$out/$role.json"
SELECT json_build_object(
         'role', pa.role, 'model', pa.model, 'digest', pa.digest,
         'archived_at', pa.created_at, 'prompt', pa.prompt)
FROM prompt_archive pa
JOIN council_decisions d ON pa.context_id = d.agenda_item_id
WHERE d.id = :'gov' AND pa.role = (:'role')::model_role_enum
ORDER BY pa.created_at
LIMIT 1;
SQL
  printf '%-12s %s bytes  sha256 %s\n' "$role" "$(wc -c < "$out/$role.json")" \
    "$(sha256sum "$out/$role.json" | cut -d' ' -f1)"
done
