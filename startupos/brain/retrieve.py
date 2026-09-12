"""Postgres full-text retrieval over brain_docs, messages and documents. Tier 0 — no embeddings, no model."""

from __future__ import annotations

from typing import Any

import psycopg

_HEADLINE_OPTS = "MaxWords=40, MinWords=15, ShortWord=3, MaxFragments=1"

_SQL = f"""
WITH q AS (SELECT plainto_tsquery('english', %(query)s) AS tsq)
SELECT * FROM (
  SELECT 'brain_docs' AS source, b.path AS id, b.path AS title,
         ts_headline('english', b.content, q.tsq, '{_HEADLINE_OPTS}') AS snippet,
         ts_rank(b.tsv, q.tsq) AS score
  FROM brain_docs b, q WHERE b.tenant_id = %(tenant)s AND b.tsv @@ q.tsq
  UNION ALL
  SELECT 'messages', m.source || ':' || m.id, coalesce(m.channel, '') || ' · ' || coalesce(m.author, ''),
         ts_headline('english', m.text, q.tsq, '{_HEADLINE_OPTS}'),
         ts_rank(m.tsv, q.tsq)
  FROM messages m, q WHERE m.tenant_id = %(tenant)s AND m.tsv @@ q.tsq
  UNION ALL
  SELECT 'documents', d.source || ':' || d.id, coalesce(d.title, ''),
         ts_headline('english', d.content, q.tsq, '{_HEADLINE_OPTS}'),
         ts_rank(d.tsv, q.tsq)
  FROM documents d, q WHERE d.tenant_id = %(tenant)s AND d.tsv @@ q.tsq
) hits
ORDER BY score DESC, source, id
LIMIT %(k)s
"""


def search(conn: psycopg.Connection, tenant_id: str, query: str, k: int = 8) -> list[dict[str, Any]]:
    """Rank brain docs, Slack messages and documents for `query`.

    Returns [{source, id, path?, title, snippet, score}] best-first. `path` is set for brain_docs hits.
    """
    if not query or not query.strip():
        return []
    rows = conn.execute(_SQL, {"query": query, "tenant": tenant_id, "k": k}).fetchall()
    out = []
    for row in rows:
        hit = {
            "source": row["source"],
            "id": row["id"],
            "title": row["title"],
            "snippet": row["snippet"],
            "score": float(row["score"]),
        }
        if row["source"] == "brain_docs":
            hit["path"] = row["id"]
        out.append(hit)
    return out
