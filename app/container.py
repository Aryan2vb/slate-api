"""Composition root: the only module that knows which concrete adapter implements each port.

Factories pick an adapter from Settings (Factory + Strategy). Tests build a Container with fakes instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.ports.providers import AudioAssembler, AuthVerifier, LanguageModel, ObjectStorage, SpeechToText
from app.ports.repositories import UnitOfWorkFactory


@dataclass(frozen=True)
class Container:
    settings: Settings
    uow: UnitOfWorkFactory
    auth: AuthVerifier
    storage: ObjectStorage
    stt: SpeechToText
    llm: LanguageModel
    audio: AudioAssembler


def build_container(settings: Settings) -> Container:
    # Imports are local so a missing optional vendor never breaks importing the app.
    from app.adapters.audio.ffmpeg import FfmpegAudioAssembler
    from app.adapters.auth.supabase import SupabaseJwtVerifier
    from app.adapters.db.uow import SqlAlchemyUnitOfWorkFactory
    from app.adapters.llm.factory import build_language_model
    from app.adapters.storage.supabase import SupabaseStorage
    from app.adapters.stt.factory import build_speech_to_text

    return Container(
        settings=settings,
        uow=SqlAlchemyUnitOfWorkFactory.from_url(settings.database_url),
        auth=SupabaseJwtVerifier.from_settings(settings),
        storage=SupabaseStorage.from_settings(settings),
        stt=build_speech_to_text(settings),
        llm=build_language_model(settings),
        audio=FfmpegAudioAssembler(),
    )
