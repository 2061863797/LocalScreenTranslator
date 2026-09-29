"""离线翻译后端与统一请求。"""

from .contracts import TranslationRequest, TranslationResult, TranslationBackend
from .router import TranslationRouter

__all__ = ["TranslationRequest", "TranslationResult", "TranslationBackend", "TranslationRouter"]
