"""Row -> domain translation. Domain models read rows via from_attributes, so mapping stays declarative."""

from app.adapters.db.tables import (
    AudioChunkRow,
    EnhancedNotesRow,
    FolderRow,
    JobRow,
    MeetingRow,
    NoteLineRow,
    ShareRow,
    SpeakerRow,
    TranscriptSegmentRow,
)
from app.domain.models import (
    AudioChunk,
    EnhancedNotes,
    Folder,
    Job,
    Meeting,
    NoteLine,
    Share,
    Speaker,
    TranscriptSegment,
)


def folder(row: FolderRow) -> Folder:
    return Folder.model_validate(row)


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


def share(row: ShareRow) -> Share:
    return Share.model_validate(row)
