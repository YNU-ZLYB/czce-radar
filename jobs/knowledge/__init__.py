"""产业链知识库（YAML → DB）。"""

from jobs.knowledge.models import KnowledgeDoc, load_knowledge_dir

__all__ = ["KnowledgeDoc", "load_knowledge_dir"]
