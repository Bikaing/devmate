# backend/core/knowledge_base.py
# 公共检索基建：BGE-M3 嵌入（dense+sparse）+ Milvus 向量库 + BGE-Reranker 精排。
# 约定：各 Agent 统一通过本模块获取嵌入/精排/向量库能力，禁止直接调底层库。
import threading
import time
from dataclasses import dataclass, field

import torch
from FlagEmbedding import BGEM3FlagModel
from pymilvus import DataType, MilvusClient
from sentence_transformers import CrossEncoder

from backend.config import get_settings
from backend.core.exceptions import MilvusConnectionError
from backend.core.logger import get_logger

logger = get_logger(__name__)

COLLECTION_NAME = "code_chunks"  # 与 PG 表同名，存放子块向量
DENSE_DIM = 1024                 # BGE-M3 稠密向量固定维度
EMBED_BATCH_SIZE = 16            # 编码批大小，过大会内存尖峰
EMBED_MAX_LENGTH = 512           # BGE-M3 最大 token 长度，超出截断


@dataclass
class EmbeddedText:
    """一条文本的编码结果：稠密向量 + 稀疏向量（token_id → 权重）。"""
    dense: list[float]
    sparse: dict[int, float] = field(default_factory=dict)


class Embedder:
    """BGE-M3 包装：懒加载（首次调用才把 2.2GB 权重读进内存）+ 线程安全单例。"""

    _model: BGEM3FlagModel | None = None
    _lock = threading.Lock()

    @classmethod
    def _load(cls) -> BGEM3FlagModel:
        if cls._model is None:
            with cls._lock:
                if cls._model is None:
                    t0 = time.time()
                    cls._model = BGEM3FlagModel(
                        get_settings().bge_m3_model_path,
                        use_fp16=torch.cuda.is_available(),  # fp16 需要 CUDA，纯 CPU 环境自动关闭
                    )
                    logger.info("knowledge_base.bge_m3_loaded", seconds=round(time.time() - t0, 1))
        return cls._model

    @classmethod
    def encode(cls, texts: list[str]) -> list[EmbeddedText]:
        """批量编码：同时返回 dense（1024 维）与 sparse（词法权重），供混合检索使用。"""
        if not texts:
            return []
        out = cls._load().encode(
            texts,
            batch_size=EMBED_BATCH_SIZE,
            max_length=EMBED_MAX_LENGTH,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,  # 多向量模式暂不启用，省内存
        )
        results = []
        for i in range(len(texts)):
            sparse = {int(tok): float(w) for tok, w in out["lexical_weights"][i].items()}
            results.append(EmbeddedText(dense=[float(x) for x in out["dense_vecs"][i]], sparse=sparse))
        return results


class Reranker:
    """BGE-Reranker-large 包装：CrossEncoder 交叉编码打分，对候选子块精排。"""

    _model: CrossEncoder | None = None
    _lock = threading.Lock()

    @classmethod
    def _load(cls) -> CrossEncoder:
        if cls._model is None:
            with cls._lock:
                if cls._model is None:
                    t0 = time.time()
                    cls._model = CrossEncoder(get_settings().reranker_model_path)
                    logger.info("knowledge_base.reranker_loaded", seconds=round(time.time() - t0, 1))
        return cls._model

    @classmethod
    def rerank(cls, query: str, docs: list[str], top_n: int | None = None) -> list[tuple[int, float]]:
        """返回 [(原文档下标, 分数)]，按分数降序；top_n 非空时截断。"""
        if not docs:
            return []
        scores = cls._load().predict([(query, d) for d in docs])
        order = sorted(range(len(docs)), key=lambda i: float(scores[i]), reverse=True)
        if top_n is not None:
            order = order[:top_n]
        return [(i, float(scores[i])) for i in order]


class VectorStore:
    """Milvus 包装：连接单例 + 双向量 collection + 幂等写入 / 按文件删除。"""

    _client: MilvusClient | None = None
    _lock = threading.Lock()

    @classmethod
    def get_client(cls) -> MilvusClient:
        if cls._client is None:
            with cls._lock:
                if cls._client is None:
                    uri = f"http://{get_settings().milvus_host}:{get_settings().milvus_port}"
                    try:
                        cls._client = MilvusClient(uri=uri)
                    except Exception as e:
                        raise MilvusConnectionError(f"Milvus 连接失败: {uri}", details={"uri": uri}) from e
                    logger.info("knowledge_base.milvus_connected", uri=uri)
        return cls._client

    @classmethod
    def ensure_collection(cls) -> None:
        """幂等建 collection：dense(HNSW/COSINE) + sparse(倒排/IP) 双索引。
        Milvus 2.4 的 schema 建好后不能加字段，故 sparse 字段一次到位。"""
        client = cls.get_client()
        if client.has_collection(COLLECTION_NAME):
            return
        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("vector_id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("repo_id", DataType.VARCHAR, max_length=64)   # 隔离轴：检索时按仓库过滤
        schema.add_field("file_id", DataType.VARCHAR, max_length=64)   # 增量索引按文件删旧向量
        schema.add_field("chunk_index", DataType.INT64)                # 检索期兄弟窗口扩展依据
        schema.add_field("start_line", DataType.INT64)                 # citations 行号取子块区间
        schema.add_field("end_line", DataType.INT64)
        schema.add_field("dense", DataType.FLOAT_VECTOR, dim=DENSE_DIM)
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)       # 词法向量：标识符类 query 的补强
        index_params = client.prepare_index_params()
        index_params.add_index(field_name="dense", index_type="HNSW", metric_type="COSINE",
                               params={"M": 16, "efConstruction": 200})
        index_params.add_index(field_name="sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="IP")
        client.create_collection(
            collection_name=COLLECTION_NAME, schema=schema, index_params=index_params,
            consistency_level="Strong",  # 默认 Bounded 有数秒读延迟，索引对账会误报，改强一致
        )
        logger.info("knowledge_base.collection_created", collection=COLLECTION_NAME)

    @classmethod
    def upsert_chunks(cls, rows: list[dict]) -> int:
        """幂等写入：vector_id 为主键，崩溃重试重跑只会覆盖、不会产生重复向量。"""
        if not rows:
            return 0
        cls.ensure_collection()
        cls.get_client().upsert(collection_name=COLLECTION_NAME, data=rows)
        return len(rows)

    @classmethod
    def delete_by_file(cls, repo_id: str, file_id: str) -> None:
        """文件级清理旧向量（增量索引「先删后插」的删除步）。"""
        cls.get_client().delete(
            collection_name=COLLECTION_NAME,
            filter=f'repo_id == "{repo_id}" and file_id == "{file_id}"',
        )

    @classmethod
    def count(cls, repo_id: str | None = None) -> int:
        """精确实体数，用于与 PG code_chunks 行数对账。"""
        flt = f'repo_id == "{repo_id}"' if repo_id else "chunk_index >= 0"
        res = cls.get_client().query(collection_name=COLLECTION_NAME, filter=flt, output_fields=["count(*)"])
        return int(res[0]["count(*)"])
