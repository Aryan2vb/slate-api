import uuid

from app.domain.models import Speaker, SpeakerSuggestion, TemplateKind
from app.services.speakers import SpeakerService, SpeakerSuggestions
from tests.fakes import FakeLlm, FakeStt, InMemoryUnitOfWorkFactory

USER = uuid.uuid4()


async def _setup(suggestions: list[SpeakerSuggestion]):
    uow = InMemoryUnitOfWorkFactory()
    async with uow() as u:
        meeting = await u.meetings.create(
            user_id=USER, title="Sync", template=TemplateKind.GENERAL, calendar_event_id=None, attendees=[]
        )
        await u.transcripts.replace(meeting_id=meeting.id, segments=FakeStt().transcript.segments)  # labels 0, 1
    llm = FakeLlm([SpeakerSuggestions(suggestions=suggestions)])
    return SpeakerService(uow, llm), uow, llm, meeting


def _s(label: int, name: str, confidence: float) -> SpeakerSuggestion:
    return SpeakerSuggestion(label=label, name=name, confidence=confidence)


async def test_saves_only_confident_suggestions_for_known_labels() -> None:
    service, uow, _, meeting = await _setup([_s(0, "Priya", 0.95), _s(1, "Maybe Sam", 0.4), _s(7, "Ghost", 0.99)])

    saved = await service.suggest(user_id=USER, meeting_id=meeting.id)

    assert [(s.label, s.name, s.confirmed) for s in saved] == [(0, "Priya", False)]
    assert set(uow.state.speakers) == {(meeting.id, 0)}


async def test_confirmed_speakers_are_never_overwritten() -> None:
    service, uow, llm, meeting = await _setup([_s(0, "Wrong Name", 0.99), _s(1, "Sam", 0.8)])
    await service.confirm(user_id=USER, meeting_id=meeting.id, label=0, name="Priya", attendee_email=None)

    await service.suggest(user_id=USER, meeting_id=meeting.id)

    assert uow.state.speakers[(meeting.id, 0)] == Speaker(meeting_id=meeting.id, label=0, name="Priya", confirmed=True)
    assert uow.state.speakers[(meeting.id, 1)].name == "Sam"
    assert "Speaker 0" not in llm.prompts[0][1].split("\n")[0]  # only unconfirmed labels are asked about


async def test_no_llm_call_when_every_label_is_confirmed() -> None:
    service, _, llm, meeting = await _setup([])
    for label in (0, 1):
        await service.confirm(user_id=USER, meeting_id=meeting.id, label=label, name=f"P{label}", attendee_email=None)

    assert await service.suggest(user_id=USER, meeting_id=meeting.id) == []
    assert llm.prompts == []
