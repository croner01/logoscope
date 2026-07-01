"""KnowledgeMemoryStore — 知识和记忆存储。"""
from typing import List, Optional
from .models import KnowledgeDocument
from .memory import MemoryRecord


class KnowledgeMemoryStore:
    """
    知识和记忆存储。

    - add_document: 添加知识文档
    - add_memory: 添加操作记忆
    - retrieve: 按关键字检索
    """

    def __init__(self):
        self._documents: dict = {}
        self._memories: dict = {}

    def add_document(self, doc: KnowledgeDocument):
        self._documents[doc.document_id] = doc

    def add_memory(self, memory: MemoryRecord):
        self._memories[memory.record_id] = memory

    def retrieve(self, query: str) -> List:
        """按关键字检索文档和记忆。"""
        query_tokens = query.lower().split()
        results = []
        for doc in self._documents.values():
            searchable = [
                doc.title.lower(), doc.content.lower(),
                doc.document_type.lower(), doc.origin.lower(),
            ]
            # 搜索 tags 和 symptoms（如果存在）
            for t in getattr(doc, "tags", []):
                searchable.append(t.lower())
            for s in getattr(doc, "symptoms", []):
                searchable.append(s.lower())
            if self._match_tokens(query_tokens, searchable):
                results.append(doc)
        for mem in self._memories.values():
            searchable = [
                mem.action_taken.lower(),
                (mem.error_message or "").lower(),
                mem.record_type.lower(),
                mem.outcome.lower(),
            ]
            if self._match_tokens(query_tokens, searchable):
                results.append(mem)
        return results

    @staticmethod
    def _match_tokens(tokens: List[str], fields: List[str]) -> bool:
        """检查是否至少有一个 token 出现在某个字段中。"""
        if not tokens:
            return False
        searchable_text = " ".join(fields)
        for token in tokens:
            if token in searchable_text:
                return True
        return False

    def get_document(self, doc_id: str) -> Optional[KnowledgeDocument]:
        return self._documents.get(doc_id)

    def get_memory(self, mem_id: str) -> Optional[MemoryRecord]:
        return self._memories.get(mem_id)
