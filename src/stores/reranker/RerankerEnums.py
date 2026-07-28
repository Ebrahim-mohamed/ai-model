from enum import Enum


class RerankerEnums(Enum):
    """Closed set of re-ranking provider adapters — mapped to a concrete
    class only inside RerankerProviderFactory."""

    CROSS_ENCODER = "CROSS_ENCODER"
