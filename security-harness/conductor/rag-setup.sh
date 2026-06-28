#!/usr/bin/env bash
# One-shot: enable vector-RAG grounding.
#   - start pgvector + create the vector extension
#   - restart the local server with a pgvector vectorDB instance configured
#   - re-register workflows and index the knowledge base
# Idempotent; safe to re-run. Requires OPENAI_API_KEY in the server's env (embeddings).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${PGVECTOR_URL:=jdbc:postgresql://localhost:5433/vectordb}"

echo "→ starting pgvector"
docker compose --profile rag up -d pgvector >/dev/null
for _ in $(seq 1 30); do docker exec sc-pgvector pg_isready -U conductor >/dev/null 2>&1 && break; sleep 1; done
docker exec sc-pgvector psql -U conductor -d vectordb -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null 2>&1 || true

echo "→ restarting server with the pgvector vectorDB instance (additive env config)"
conductor server stop >/dev/null 2>&1 || true
env -u CONDUCTOR_CONFIG_FILE \
  CONDUCTOR_VECTORDB_INSTANCES_0_NAME=pgvector \
  CONDUCTOR_VECTORDB_INSTANCES_0_TYPE=postgres \
  CONDUCTOR_VECTORDB_INSTANCES_0_POSTGRES_DATASOURCEURL="$PGVECTOR_URL" \
  CONDUCTOR_VECTORDB_INSTANCES_0_POSTGRES_USER=conductor \
  CONDUCTOR_VECTORDB_INSTANCES_0_POSTGRES_PASSWORD=conductor \
  CONDUCTOR_VECTORDB_INSTANCES_0_POSTGRES_DIMENSIONS=1536 \
  conductor server start >/dev/null

echo "→ re-registering workflows + indexing the knowledge base"
bash "$ROOT/conductor/register.sh" >/dev/null
input=$(jq -n --arg kb "$ROOT/knowledge/owasp-remediation.md" \
  '{kb_path:$kb, vectorDB:"pgvector", namespace:"kb", index:"sc", provider:"openai", model:"text-embedding-3-small"}')
conductor workflow start -w rag_index -i "$input" >/dev/null
echo "✓ RAG enabled. Run workers with the 'rag' module; scans now retrieve KB chunks at triage."
