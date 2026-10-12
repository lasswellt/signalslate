"""Request/response models shared by more than one router."""
from pydantic import BaseModel, ConfigDict, Field

# A pasted CSV import body is capped well above any real watchlist or portfolio (one line per
# company or domain) but far short of a request that could tie up the worker thread parsing it.
MAX_IMPORT_CHARS = 200_000


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csv: str = Field(max_length=MAX_IMPORT_CHARS)


class RejectedRow(BaseModel):
    line: int
    reason: str


class ImportOut(BaseModel):
    added: list[str]
    rejected: list[RejectedRow]

    @classmethod
    def from_result(cls, result: object) -> "ImportOut":
        """Builds the response from a pipeline ImportResult (`.added`, `.rejected` of (line, reason))."""
        added = list(getattr(result, "added"))
        rejected = [RejectedRow(line=line, reason=reason) for line, reason in getattr(result, "rejected")]
        return cls(added=added, rejected=rejected)
