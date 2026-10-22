"""Recover a broken Milvus collection from PG chunks, preserving the old collection.

Run from the repository root: python -m scripts.rebuild_vector_index
This uses local embedding weights and does not call an external model API.
"""
from collections import Counter
from datetime import datetime, timezone
import uuid

import psycopg
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.core.knowledge_base import COLLECTION_NAME, Embedder, VectorStore


def main():
    settings = get_settings()
    client = VectorStore.get_client()
    suffix = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
    staging = f'{COLLECTION_NAME}_rebuild_{suffix}'
    backup = f'{COLLECTION_NAME}_backup_{suffix}'
    with psycopg.connect(host=settings.db_host, port=settings.db_port,
                         dbname=settings.db_name, user=settings.db_user,
                         password=settings.db_password, row_factory=dict_row) as conn:
        # Keep new indexing claims out until the snapshot has been published.
        repos = conn.execute('SELECT id, index_status FROM repos ORDER BY id FOR UPDATE').fetchall()
        if any(r['index_status'] == 'indexing' for r in repos):
            raise RuntimeError('An indexing task is active; retry after it finishes.')
        chunks = conn.execute(
            'SELECT vector_id, repo_id, file_id, chunk_index, start_line, end_line, content '
            'FROM code_chunks ORDER BY repo_id, file_id, chunk_index'
        ).fetchall()
        VectorStore.ensure_collection(staging)
        print(f'Created staging collection: {staging}', flush=True)
        for start in range(0, len(chunks), 32):
            batch = chunks[start:start + 32]
            embeddings = Embedder.encode([row['content'] for row in batch])
            if len(embeddings) != len(batch):
                raise RuntimeError('Embedding result count mismatch')
            rows = [dict(vector_id=str(row['vector_id']), repo_id=str(row['repo_id']),
                         file_id=str(row['file_id']), chunk_index=row['chunk_index'],
                         start_line=row['start_line'], end_line=row['end_line'],
                         dense=emb.dense, sparse=emb.sparse)
                    for row, emb in zip(batch, embeddings)]
            client.upsert(staging, rows, timeout=30)
            print(f'Rebuilt {min(start + 32, len(chunks))}/{len(chunks)}', flush=True)
        expected = Counter(str(row['repo_id']) for row in chunks)
        for repo in repos:
            rid = str(repo['id'])
            actual = client.query(staging, filter=f'repo_id == "{rid}"',
                                  output_fields=['count(*)'], timeout=15)[0]['count(*)']
            if actual != expected[rid]:
                raise RuntimeError(f'Vector count mismatch for {rid}: {actual} != {expected[rid]}')
        had_old = client.has_collection(COLLECTION_NAME)
        if had_old:
            client.rename_collection(COLLECTION_NAME, backup, timeout=15)
        try:
            client.rename_collection(staging, COLLECTION_NAME, timeout=15)
        except Exception:
            if had_old:
                client.rename_collection(backup, COLLECTION_NAME, timeout=15)
            raise
        print(f'Restored {len(chunks)} chunks; backup: {backup if had_old else "none (fresh namespace)"}', flush=True)


if __name__ == '__main__':
    main()
