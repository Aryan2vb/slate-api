from app.config import Settings
from app.ports.providers import LanguageModel


def build_language_model(settings: Settings) -> LanguageModel:
    match settings.llm_provider:
        case "groq":
            from app.adapters.llm.openai_compat import OpenAICompatibleLanguageModel

            return OpenAICompatibleLanguageModel(settings.groq_api_key.get_secret_value(), model=settings.llm_model)
        case "openai-compatible":
            from app.adapters.llm.openai_compat import OpenAICompatibleLanguageModel

            if not settings.llm_base_url or not settings.llm_model:
                raise ValueError("LLM_PROVIDER=openai-compatible requires LLM_BASE_URL and LLM_MODEL")
            return OpenAICompatibleLanguageModel(
                settings.llm_api_key.get_secret_value(),
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                provider="openai-compatible",
            )
        case "anthropic":
            from app.adapters.llm.anthropic import AnthropicLanguageModel

            return AnthropicLanguageModel(settings.anthropic_api_key.get_secret_value(), model=settings.llm_model)
        case "fake":
            from app.adapters.llm.stub import StubLanguageModel

            return StubLanguageModel()
