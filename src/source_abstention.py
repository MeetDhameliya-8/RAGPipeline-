"""A typed empty source response is a review hold, never accepted content."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AbstentionArticle(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_file: str
    topic: str
    subtopic: str
    reference: str
    number_of_chunks: Literal[0]


class SourceAbstention(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    article: AbstentionArticle
    chunks: list[dict] = Field(max_length=0)


def is_source_abstention(candidate: dict, source_file: str) -> bool:
    if not isinstance(candidate, dict) or not isinstance(candidate.get('article'), dict):
        return False
    if type(candidate['article'].get('number_of_chunks')) is not int:
        return False
    try:
        response = SourceAbstention.model_validate(candidate, strict=True)
    except ValueError:
        return False
    return response.article.source_file == source_file
