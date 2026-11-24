"""Provider implementations wrapping CLI platforms and external Model APIs."""

from engines.providers.antigravity import AntigravityEngineAdapter
from engines.providers.gemini import GoogleGeminiAdapter

__all__ = [
    "AntigravityEngineAdapter",
    "GoogleGeminiAdapter",
]
