"""可丢弃的本地向量缓存；仅保存内容摘要和向量，不保存源码。"""
from array import array
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time


def model_fingerprint(model_path: str, precision: str, max_length: int) -> str:
    root = Path(model_path).resolve()
    files = []
    # 权重、分词器或配置变化后自动使用新的命名空间，无需读取数 GB 权重做哈希。
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if path.is_file() and path.suffix in {'.bin', '.safetensors', '.pt', '.json', '.model'}:
                stat = path.stat()
                files.append((path.name, stat.st_size, stat.st_mtime_ns))
    identity = ('bge-m3-dense-sparse-v1', str(root), files, precision, max_length, sys.byteorder)
    return hashlib.sha256(repr(identity).encode()).hexdigest()


class EmbeddingCache:
    def __init__(self, path: str, namespace: str, max_entries: int = 10000):
        self.path, self.namespace, self.max_entries = Path(path), namespace, max_entries

    def key(self, text):
        return self.namespace + ':' + hashlib.sha256(text.encode('utf-8')).hexdigest()

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE IF NOT EXISTS embeddings '
                     '(key TEXT PRIMARY KEY, dense BLOB NOT NULL, sparse TEXT NOT NULL, touched REAL NOT NULL)')
        return conn

    def get_many(self, texts):
        result = {}
        by_key = {self.key(t): t for t in texts}
        conn = self.connect()
        try:
            keys = list(by_key)
            for start in range(0, len(keys), 400):
                batch = keys[start:start + 400]
                marks = ','.join('?' for _ in batch)
                for key, dense, sparse in conn.execute(
                        f'SELECT key,dense,sparse FROM embeddings WHERE key IN ({marks})', batch):
                    try:
                        vector = array('f')
                        vector.frombytes(dense)
                        if len(vector) != 1024:
                            continue
                        weights = {int(k): float(v) for k, v in json.loads(sparse).items()}
                        result[by_key[key]] = (list(vector), weights)
                    except (ValueError, TypeError, AttributeError):
                        continue  # 损坏的条目按未命中处理，重新计算。
            if result:
                conn.executemany('UPDATE embeddings SET touched=? WHERE key=?',
                                 [(time.time(), self.key(t)) for t in result])
                conn.commit()
        finally:
            conn.close()
        return result

    def put_many(self, values):
        conn = self.connect()
        try:
            with conn:
                conn.executemany('INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?)',
                    [(self.key(t), array('f', dense).tobytes(), json.dumps(sparse), time.time())
                     for t, (dense, sparse) in values.items()])
                conn.execute('DELETE FROM embeddings WHERE key IN '
                    '(SELECT key FROM embeddings ORDER BY touched DESC LIMIT -1 OFFSET ?)',
                    (self.max_entries,))
        finally:
            conn.close()
