"""Row -> domain translation. Domain models read rows via from_attributes, so mapping stays declarative."""

from app.adapters.db.tables import (
    AudioChunkRow,
    EnhancedNotesRow,
    JobRow,
    MeetingRow,
    NoteLineRow,
    SpeakerRow,
    TranscriptSegmentRow,
)
from app.domain.models import (
    AudioChunk,
    EnhancedNotes,
    Job,
    Meeting,
    NoteLine,
    Speaker,
    TranscriptSegment,
)


def meeting(row: MeetingRow) -> Meeting:
    return Meeting.model_validate(row)


def note_line(row: NoteLineRow) -> NoteLine:
    return NoteLine.model_validate(row)


def audio_chunk(row: AudioChunkRow) -> AudioChunk:
    return AudioChunk.model_validate(row)


def transcript_segment(row: TranscriptSegmentRow) -> TranscriptSegment:
    return TranscriptSegment.model_validate(row)


def speaker(row: SpeakerRow) -> Speaker:
    return Speaker.model_validate(row)


def enhanced_notes(row: EnhancedNotesRow) -> EnhancedNotes:
    return EnhancedNotes.model_validate(row.content)


def job(row: JobRow) -> Job:
    return Job.model_validate(row)
