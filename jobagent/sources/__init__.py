"""Job source adapters. Add a new source by adding one file and registering it here."""

from jobagent.sources.base import FetchResult, SourceAdapter, SourceError
from jobagent.sources.greenhouse import GreenhouseAdapter
from jobagent.sources.lever import LeverAdapter

ADAPTERS: dict[str, type[SourceAdapter]] = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
}

__all__ = ["ADAPTERS", "FetchResult", "SourceAdapter", "SourceError"]
