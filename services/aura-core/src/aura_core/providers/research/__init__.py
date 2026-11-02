"""Provider-neutral ports for governed web research.

The research coordinator depends on the small typed ports exported here.  The
HTTP implementation is deliberately kept in this package and has no access to
research persistence or policy tables.
"""

from .client import (
    BrowserFetchProvider,
    ProviderClientError,
    ResearchProviderHealth,
    ResearchProviderUnavailable,
    SearchProvider,
    StaticFetchProvider,
)
from .contracts import (
    BrowserFetchRequest,
    FetchEvidenceResult,
    SearchRequest,
    SearchResult,
)
from .evaluation import (
    ResearchEvaluationResult,
    ResearchEvaluationSuite,
    run_research_evaluation_fixtures,
)
from .signing import EnvelopeSigner, EnvelopeVerificationError, ReplayCache

__all__ = [
    "BrowserFetchProvider",
    "BrowserFetchRequest",
    "EnvelopeSigner",
    "EnvelopeVerificationError",
    "FetchEvidenceResult",
    "ProviderClientError",
    "ResearchProviderHealth",
    "ResearchProviderUnavailable",
    "ReplayCache",
    "ResearchEvaluationResult",
    "ResearchEvaluationSuite",
    "SearchProvider",
    "SearchRequest",
    "SearchResult",
    "StaticFetchProvider",
    "run_research_evaluation_fixtures",
]
