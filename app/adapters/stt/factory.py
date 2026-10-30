from app.config import Settings
from app.ports.providers import SpeechToText


def build_speech_to_text(settings: Settings) -> SpeechToText:
    match settings.stt_provider:
        case "deepgram":
            from app.adapters.stt.deepgram import DeepgramSpeechToText

            return DeepgramSpeechToText(settings.deepgram_api_key.get_secret_value(), language=settings.stt_language)
        case "groq":
            from app.adapters.stt.groq_whisper import GroqWhisperSpeechToText

            return GroqWhisperSpeechToText(settings.groq_api_key.get_secret_value())
        case "fake":
            from app.adapters.stt.stub import StubSpeechToText

            return StubSpeechToText()
