from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VerificationSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=500)
    doi: str | None = Field(default=None, max_length=300)
    pmid: str | None = Field(default=None, max_length=32)
    openalex_id: str | None = Field(default=None, max_length=100)
    arxiv_id: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^(?:\d{4}\.\d{4,5}|[A-Za-z-]+(?:\.[A-Za-z-]+)?/\d{7})(?:v\d+)?$",
    )
    url: str | None = Field(default=None, max_length=2_000)
    cited_as: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def require_identifier(self) -> "VerificationSource":
        if not any((self.doi, self.pmid, self.openalex_id, self.arxiv_id, self.url)):
            raise ValueError("source_identifier_required")
        return self


class DocumentVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=80, max_length=100_000)
    sources: list[VerificationSource] = Field(default_factory=list, max_length=40)
