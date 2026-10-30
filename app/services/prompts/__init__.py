"""Prompt text and the formatting of prompt inputs. Kept apart from services so wording changes never touch logic."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.domain.models import Attendee, NoteLine, TemplateKind, TranscriptSegment


def format_ms(ms: int) -> str:
    seconds = max(ms, 0) // 1000
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def format_transcript(segments: Sequence[TranscriptSegment], names: Mapping[int, str] | None = None) -> str:
    names = names or {}
    lines = []
    for s in segments:
        who = names.get(s.speaker, f"Speaker {s.speaker}")
        lines.append(f"[{format_ms(s.start_ms)}] ({s.start_ms}-{s.end_ms}ms) {who}: {s.text}")
    return "\n".join(lines)


def format_notes(lines: Sequence[NoteLine]) -> str:
    return "\n".join(
        f"[{format_ms(n.t_ms) if n.t_ms is not None else '--:--'}] {n.text}" for n in lines if n.text.strip()
    )


def format_attendees(attendees: Sequence[Attendee]) -> str:
    rows = [f"- {a.name or '(no name)'} <{a.email or 'no email'}>" for a in attendees]
    return "\n".join(rows) or "(no attendee list)"


TEMPLATE_GUIDANCE: dict[TemplateKind, str] = {
    TemplateKind.GENERAL: (
        "Organise by topic in the order discussed. Capture decisions, action items and open questions."
    ),
    TemplateKind.ONE_ON_ONE: (
        "This is a 1:1. Sections: updates, blockers, feedback given or received, career or growth topics, "
        "and agreed next steps."
    ),
    TemplateKind.SALES: (
        "This is a sales call. Sections: customer context, pain points, current solution, budget and timeline, "
        "decision makers, objections, and next steps."
    ),
    TemplateKind.INTERVIEW: (
        "This is an interview. Sections: candidate background, questions asked with a summary of each answer, "
        "strengths, concerns. Report what was said; do not give a hiring verdict."
    ),
    TemplateKind.LECTURE: (
        "This is a lecture. Sections follow the lecture's topics. Define key terms, keep examples and formulas, "
        "and list anything the lecturer flagged as important or likely to be examined."
    ),
}


ENHANCE_SYSTEM = """\
You turn a person's rough meeting notes into complete, well-organised notes, using the meeting transcript.

The person's own notes are the outline: they show what mattered to them. Rules:
1. Every non-empty line the person wrote must appear exactly as written, as a block with source "user". Do not \
reword, fix or merge them. Place each under the section where it belongs.
2. Add context from the transcript as blocks with source "ai". Every "ai" block must cite one or more evidence \
ranges (start_ms/end_ms) copied from the transcript lines it is based on.
3. Never state anything the transcript does not support. If something is unclear, put it in open_questions.
4. Action items: who, what, and a due date only if one was said aloud. Decisions: only ones actually agreed.
5. Omit empty sections. Be concise: prefer short, specific bullets over paraphrasing the whole conversation.
6. The tldr is two sentences at most."""


def enhance_prompt(*, title: str, template: TemplateKind, attendees: str, notes: str, transcript: str) -> str:
    return f"""\
Meeting: {title}
Type guidance: {TEMPLATE_GUIDANCE[template]}

Attendees:
{attendees}

The person's notes ([mm:ss] is when they typed the line during the recording):
<notes>
{notes or "(no notes taken: write the notes from the transcript alone)"}
</notes>

Transcript ([mm:ss] (start-end ms) speaker: text):
<transcript>
{transcript}
</transcript>"""


SPEAKERS_SYSTEM = """\
You identify who is speaking in a diarized meeting transcript. Speakers are labelled "Speaker N".
Use only evidence in the transcript: self-introductions ("I'm Priya"), being addressed by name right before \
speaking ("Priya, what do you think?" followed by that speaker), and the attendee list.
Return a suggestion only for labels you have evidence for. Confidence: 0.9+ for a self-introduction, about 0.7 \
for being addressed by name, lower otherwise. Include the start_ms of the lines that are your evidence. Prefer \
names from the attendee list and include their email when you match one. Never give two labels the same person."""


def speakers_prompt(*, attendees: str, transcript: str, labels: Sequence[int]) -> str:
    return f"""\
Labels to identify: {", ".join(f"Speaker {label}" for label in labels)}

Attendees:
{attendees}

Transcript:
<transcript>
{transcript}
</transcript>"""


CATCHUP_SYSTEM = """\
Someone just lost focus in a live meeting and asks "what did I miss?". Summarise only the excerpt given: one \
short sentence of summary and at most five points, most important first. Use only what the excerpt says."""


def catchup_prompt(*, title: str, minutes: int, excerpt: str) -> str:
    return f"""\
Meeting: {title}
The last {minutes} minutes:
<excerpt>
{excerpt}
</excerpt>"""
