from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class KeywordResearchResult:
    keyword: str
    source: str
    source_status: str
    query_url: str
    searched_at: str
    result_count: int
    results: List[Dict[str, Any]] = field(default_factory=list)
    notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
