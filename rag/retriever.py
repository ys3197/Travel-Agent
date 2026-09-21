"""
RAG 检索器：query → Chroma 向量搜索 → 返回相关游记 chunks。
支持 MMR（Maximal Marginal Relevance）增加结果多样性。
"""

import logging
import random
from pathlib import Path

import chromadb
import numpy as np
from sentence_transformers import SentenceTransformer

from rag.poi_names import POI_ALIASES, normalize_poi

logger = logging.getLogger(__name__)

DB_DIR = Path(__file__).parent / "data" / "db"
COLLECTION_NAME = "travel_notes"
EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# ── 景点名 → 语料标题的精确解析 ────────────────────────────────
# 语料本身是按实体组织的（每条记录的 title 就是景点名），所以按景点名取资料
# 属于"已知确切 token"的场景，用元数据精确查找而非向量检索：更快、可解释，
# 且查不到就是真的没有——而向量检索永远会返回"最近的某一段"，哪怕它讲的是别的景点。
# 归一化规则见 rag/poi_names.py（离线的 build_poi_table 共用同一套）。
_normalize_poi = normalize_poi


def _mmr(
    query_vec: np.ndarray,
    candidates: list[dict],
    top_k: int,
    lambda_: float,
) -> list[dict]:
    """
    Maximal Marginal Relevance 选取多样化子集。
    lambda_: 0=最大多样性，1=最大相关性
    """
    selected, remaining = [], list(candidates)

    while len(selected) < top_k and remaining:
        if not selected:
            # 第一个选相关性最高的
            best = max(remaining, key=lambda c: c["score"])
        else:
            selected_embs = np.stack([c["_emb"] for c in selected])

            def mmr_score(c):
                relevance = c["score"]
                # 与已选结果的最大相似度（惩罚项）
                sim_to_selected = float(np.max(selected_embs @ c["_emb"]))
                return lambda_ * relevance - (1 - lambda_) * sim_to_selected

            best = max(remaining, key=mmr_score)

        selected.append(best)
        remaining.remove(best)

    return selected


class NoteRetriever:
    def __init__(self):
        if not DB_DIR.exists():
            raise FileNotFoundError("Chroma DB not found. Run `python -m rag.ingest` first.")

        self._model = None   # 懒加载：retrieve_by_poi() 不需要 embedding 模型
        client = chromadb.PersistentClient(path=str(DB_DIR))
        self.collection = client.get_collection(COLLECTION_NAME)

        # 规范化标题 → 语料中的真实标题。一次性建好，之后解析景点名是纯内存查表。
        self._titles: set[str] = set()
        self._title_index: dict[str, str] = {}
        metas = self.collection.get(include=["metadatas"])["metadatas"] or []
        for m in metas:
            title = m.get("title")
            if not title:
                continue
            self._titles.add(title)
            norm = _normalize_poi(title)
            prev = self._title_index.get(norm)
            # 归一化撞车时（'Bristol Mountain' vs 'Bristol Mountain Ski Resort'）
            # 固定取更短的那个，保证结果与 Chroma 的返回顺序无关
            if prev is None or len(title) < len(prev):
                self._title_index[norm] = title

        logger.info(
            f"Retriever ready: {self.collection.count()} vectors, "
            f"{len(self._title_index)} distinct POIs indexed"
        )

    @property
    def model(self) -> SentenceTransformer:
        """embedding 模型按需加载——只做精确查找的调用方不必付这个代价。"""
        if self._model is None:
            logger.info(f"Loading embedding model {EMBED_MODEL}")
            self._model = SentenceTransformer(EMBED_MODEL)
        return self._model

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        style_tags: list[str] | None = None,
        region: str | None = "rochester_ny",
        use_mmr: bool = True,
        mmr_lambda: float = 0.6,
    ) -> list[dict]:
        """
        检索与 query 最相关的游记 chunks。

        use_mmr: 启用 MMR，减少结果同质化，每次请求返回更多样的游记组合
        mmr_lambda: 0=最大多样性，1=最大相关性，0.6 是平衡点
        style_tags: 如 ["穷游"]，只返回包含该标签的结果
        region: 限定地区
        """
        vec = self.model.encode([query], normalize_embeddings=True)[0]

        # 构建 Chroma where 过滤条件
        where: dict | None = None
        conditions = []
        if region:
            conditions.append({"region": {"$eq": region}})
        if style_tags:
            conditions.append({"tags": {"$contains": style_tags[0]}})
        if len(conditions) == 1:
            where = conditions[0]
        elif len(conditions) > 1:
            where = {"$and": conditions}

        # 多取候选，MMR 从中挑多样化子集
        fetch_k = min(top_k * 4, self.collection.count())
        results = self.collection.query(
            query_embeddings=[vec.tolist()],
            n_results=fetch_k,
            where=where,
            include=["documents", "metadatas", "distances", "embeddings"],
        )

        candidates = []
        for doc, meta, dist, emb in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
            results["embeddings"][0],
        ):
            candidates.append({
                **meta,
                "chunk_text": doc,
                "score": round(1 - dist, 4),
                "_emb": np.array(emb),
            })

        if not candidates:
            return []

        if use_mmr and len(candidates) > top_k:
            selected = _mmr(vec, candidates, top_k, mmr_lambda)
        else:
            selected = candidates[:top_k]

        return [{k: v for k, v in c.items() if k != "_emb"} for c in selected]

    # ── 按景点名精确取资料 ────────────────────────────────────
    def resolve_poi(self, name: str) -> tuple[str | None, str]:
        """
        把外部景点名解析到语料标题。返回 (标题, 命中方式)；解析不到返回 (None, "miss")。
        只做确定性匹配，不做模糊/前缀匹配——解析不到就是语料里真的没有这个景点。
        """
        if name in self._titles:
            return name, "exact"

        norm = _normalize_poi(name)
        if norm in self._title_index:
            return self._title_index[norm], "normalized"

        alias = POI_ALIASES.get(norm) or POI_ALIASES.get(name.lower().strip())
        if alias:
            resolved = self._title_index.get(_normalize_poi(alias))
            if resolved:
                return resolved, "alias"

        return None, "miss"

    def retrieve_by_poi(self, name: str, top_k: int = 1) -> list[dict]:
        """
        按景点名精确取该景点的资料片段，不走向量检索。

        调用方已经握着确切的景点名，不需要语义"理解"；元数据过滤更快、可解释，
        而且查不到时返回空列表——调用方据此可以明确知道"没有资料"，
        而不是拿到一段讲别的景点的文字。

        返回按 chunk_index 排序的前 top_k 段：chunk 0 是 Wikipedia 的导语段 /
        VisitRochester 的条目简介，正是最适合做简介的那一段。
        """
        title, how = self.resolve_poi(name)
        if title is None:
            logger.debug(f"retrieve_by_poi: no corpus entry for {name!r}")
            return []

        res = self.collection.get(
            where={"title": {"$eq": title}},
            include=["documents", "metadatas"],
        )
        docs, metas = res.get("documents") or [], res.get("metadatas") or []
        if not docs:
            return []

        rows = [
            {**m, "chunk_text": d, "score": 1.0, "match": how}
            for d, m in zip(docs, metas)
        ]
        rows.sort(key=lambda r: r.get("chunk_index", 0))

        if how != "exact":
            logger.info(f"retrieve_by_poi: {name!r} → {title!r} ({how})")
        return rows[:top_k]

    def format_context(self, chunks: list[dict]) -> str:
        """把检索结果格式化为 Planner Agent 的 prompt 上下文。"""
        if not chunks:
            return ""
        # 随机打乱顺序，避免模型总是优先参考第一篇游记
        shuffled = list(chunks)
        random.shuffle(shuffled)
        parts = []
        for i, c in enumerate(shuffled, 1):
            budget = c["budget_hint"] if c["budget_hint"] != -1 else "未知"
            parts.append(
                f"【参考游记 {i}】{c['title']}\n"
                f"标签: {c['tags'] or '无'} | 预算参考: {budget}元\n"
                f"{c['chunk_text']}"
            )
        return "\n\n".join(parts)
