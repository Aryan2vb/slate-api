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


LANGUAGE_NAMES = {
    "en": "English",
    "hi": "Hindi",
    "mr": "Marathi",
    "bn": "Bengali",
    "ta": "Tamil",
    "te": "Telugu",
    "gu": "Gujarati",
    "kn": "Kannada",
    "ml": "Malayalam",
    "pa": "Punjabi",
    "ur": "Urdu",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "pt": "Portuguese",
    "it": "Italian",
    "nl": "Dutch",
    "ru": "Russian",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "ar": "Arabic",
}


def language_rule(languages: Sequence[str]) -> str:
    """Tells the model which language to write in: the one the meeting was held in, mixed the way it was spoken."""
    named = [LANGUAGE_NAMES.get(code.split("-")[0], code) for code in languages]
    detected = f"Speech recognition detected: {', '.join(named)}." if named else ""
    return (
        f"{detected} Write every field you generate in the language the participants actually spoke, as it "
        "appears in the transcript. If they mixed languages (for example Hindi with English words, often called "
        "Hinglish), mix them the same way and keep each word in the script the transcript uses. Do not translate "
        "to English unless the meeting was in English. Names, numbers, dates and product terms stay as spoken."
    ).strip()


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
6. The tldr is two sentences at most.

Write notes, not a transcript:
7. Each "ai" block is one short point in your own words, not a copied transcript sentence. Group figures and \
specifications together. Keep definitions, examples and formulas, and keep the speaker's framing, motivation and \
conclusion as short points rather than dropping them.

The transcript is machine speech recognition, so it contains misheard words. Read it as a listener would:
8. Names, model numbers, acronyms and jargon are often split into sound-alike words ("L R one parser" for \
"LR(1) parser", "cube control" for "kubectl", "EV toll aircraft" for "eVTOL aircraft"), and short phrases can turn \
into a different phrase that sounds the same. When the sound and the context together make the intended words \
clear, or the person typed the term in their notes, write the intended words. Never repeat a phrase that makes no \
sense in context; if the intended words are unclear, leave the point out or add it to open_questions.
9. If a passage repeats a few seconds after itself word for word (a stalled or replayed video, or a recognition \
glitch), use it once.

Stay faithful to what was said:
10. Do not strengthen, generalise or add specifics: "systems" does not become "cells", "a speed of" does not \
become "cruises at". Do not attribute intentions or requests nobody voiced. Numbers and units exactly as spoken."""


def enhance_prompt(
    *, title: str, template: TemplateKind, attendees: str, notes: str, transcript: str, languages: Sequence[str] = ()
) -> str:
    return f"""\
Meeting: {title}
Type guidance: {TEMPLATE_GUIDANCE[template]}
Language: {language_rule(languages)} The person's own lines are always kept exactly as written, in whatever \
language they used.

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


# Long meetings are enhanced in parts (map) and the parts combined (reduce), so no single request has to carry the
# whole transcript. Each part follows the same rules as a whole meeting.
ENHANCE_WINDOW_SYSTEM = (
    ENHANCE_SYSTEM
    + """

This transcript is one part of a longer meeting; other parts are handled separately and merged afterwards:
11. Write notes for this part only. Include only the person's lines given here; the rest belong to other parts.
12. The title may be provisional and the tldr covers this part. Evidence ranges must come from this part."""
)


def enhance_window_prompt(
    *,
    part: int,
    parts: int,
    title: str,
    template: TemplateKind,
    attendees: str,
    notes: str,
    transcript: str,
    languages: Sequence[str] = (),
) -> str:
    whole = enhance_prompt(
        title=title, template=template, attendees=attendees, notes=notes, transcript=transcript, languages=languages
    )
    return f"Part {part} of {parts} of the meeting.\n{whole}"


MERGE_SYSTEM = """\
You combine partial notes, written for consecutive parts of one meeting, into one set of notes with the same schema.
1. Keep every block with source "user" exactly as written, once. Do not reword, fix or merge them.
2. Merge sections that cover the same topic, keeping topics in the order they were discussed. Remove points that \
repeat across parts (parts overlap slightly), keeping the clearer one.
3. Keep every evidence range exactly as given; when two blocks are combined, keep the evidence of both. Never \
invent evidence or content: use only what the partial notes say.
4. Combine action items, decisions and open questions, removing duplicates. Drop an open question that a later \
part answers.
5. Write one title for the whole meeting and a tldr of two sentences at most.
6. Keep the language the partial notes are written in; do not translate."""


def merge_prompt(
    *, title: str, template: TemplateKind, partials: str, parts: int, languages: Sequence[str] = ()
) -> str:
    return f"""\
Meeting: {title}
Type guidance: {TEMPLATE_GUIDANCE[template]}
Language: {language_rule(languages)}

Partial notes for {parts} consecutive parts of the meeting, in order (JSON):
<partials>
{partials}
</partials>"""


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


def catchup_prompt(*, title: str, minutes: int, excerpt: str, languages: Sequence[str] = ()) -> str:
    return f"""\
Meeting: {title}
Language: {language_rule(languages)}
The last {minutes} minutes:
<excerpt>
{excerpt}
</excerpt>"""
