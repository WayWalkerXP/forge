"""The encoding rules: how a source maps to a conversion target.

This module is **authoritative**, not advisory.  In the GUI this replaces, the
equivalent function computed a bitrate that the planner then ignored in favour
of whatever was in a dropdown, so the rules were really just a prefill.  Here
the rules decide, and ``forge run`` obeys them.

The rules are alchemist's, which differ from the GUI's in one important way:
they **decline** sources too poor to be worth re-encoding, rather than always
producing a number.  Re-encoding a 16 kbps file gains nothing and loses
quality, so declining is the right answer; a declined book tagged ``downcode``
becomes ``needs-attention`` for a human to look at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .disposition import Disposition
from .probe import AudioInfo

#: Bitrates forge is willing to produce, in kbps.
VALID_BITRATES: tuple[int, ...] = (32, 48, 64, 96, 128, 256, 384)

#: Below these normalised source bitrates, re-encoding is not worthwhile.
MIN_MONO_KBPS = 32
MIN_STEREO_KBPS = 64

#: The bitrate a dramatic production keeps when the source is generous.
DRAMA_HIGH_KBPS = 128
DRAMA_LOW_KBPS = 64

#: Phrases that mark a full-cast or dramatised production, which should keep
#: stereo and a higher bitrate because the stereo field carries real content.
DRAMATIC_PHRASES = (
    "graphicaudio",
    "graphic audio",
    "dramatic audio",
    "audio drama",
    "dramatised",
    "dramatized",
    "full cast",
    "full-cast",
)


@dataclass(frozen=True)
class Target:
    """The decided encoding target, or a decline with its reason."""

    bitrate_kbps: int | None
    channels: int | None
    rationale: str

    @property
    def declined(self) -> bool:
        return self.bitrate_kbps is None or self.channels is None

    @property
    def channel_description(self) -> str:
        return "mono" if self.channels == 1 else "stereo"

    def __str__(self) -> str:
        if self.declined:
            return f"declined: {self.rationale}"
        return f"{self.bitrate_kbps}k {self.channel_description}"


@dataclass(frozen=True)
class DramaticMatch:
    """Why a source looks like a dramatised production."""

    field: str
    value: str
    phrase: str


def decide(
    info: AudioInfo,
    disposition: Disposition,
    *,
    max_bitrate_kbps: int = 64,
) -> Target:
    """Decide the encoding target for *info* under *disposition*.

    ``remux`` and ``as-is`` need no encoding decision, so they report the source
    values unchanged.
    """

    if disposition is Disposition.REMUX:
        return Target(
            bitrate_kbps=info.bitrate_kbps,
            channels=info.channels,
            rationale="remux copies the audio stream unchanged",
        )
    if disposition is Disposition.AS_IS:
        return Target(
            bitrate_kbps=info.bitrate_kbps,
            channels=info.channels,
            rationale="as-is relocates the file without re-encoding",
        )
    if disposition is Disposition.DRAMA:
        return _decide_drama(info, max_bitrate_kbps)
    if disposition is Disposition.DOWNCODE:
        return _decide_downcode(info, max_bitrate_kbps)

    return Target(None, None, f"{disposition} is not a converting disposition")


def _decide_drama(info: AudioInfo, max_bitrate_kbps: int) -> Target:
    """Dramatised audio keeps stereo when the source has it."""

    source = info.normalized_bitrate_kbps

    if info.channels > 1:
        chosen = DRAMA_HIGH_KBPS if source >= DRAMA_HIGH_KBPS else DRAMA_LOW_KBPS
        # Deliberately not capped by max_bitrate_kbps: that cap exists to shrink
        # ordinary narration, and applying it here would defeat the point of
        # marking a book as dramatised.
        return Target(
            bitrate_kbps=chosen,
            channels=2,
            rationale=(
                f"dramatised, stereo source at {source}k -> {chosen}k stereo preserved"
            ),
        )

    # A mono source has no stereo field to protect, so the standard rules apply.
    target = _decide_downcode(info, max_bitrate_kbps)
    if target.declined:
        return target
    return Target(
        target.bitrate_kbps,
        target.channels,
        f"dramatised but the source is mono; {target.rationale}",
    )


def _decide_downcode(info: AudioInfo, max_bitrate_kbps: int) -> Target:
    """Standard narration: downmix to mono and cut the bitrate."""

    source = info.normalized_bitrate_kbps

    if info.channels > 1:
        if source < MIN_STEREO_KBPS:
            return Target(
                None,
                None,
                f"stereo source is only {source}k, below the {MIN_STEREO_KBPS}k "
                f"floor; re-encoding would lose quality for no gain",
            )
        if source >= 128:
            chosen = min(64, max_bitrate_kbps)
            return Target(
                chosen,
                1,
                f"stereo source at {source}k -> {chosen}k mono",
            )
        halved = _floor_to_valid(max(round(source / 2), MIN_MONO_KBPS))
        chosen = min(halved, max_bitrate_kbps)
        return Target(
            chosen,
            1,
            f"stereo source at {source}k -> half rate, {chosen}k mono",
        )

    if source < MIN_MONO_KBPS:
        return Target(
            None,
            None,
            f"mono source is only {source}k, below the {MIN_MONO_KBPS}k floor; "
            f"re-encoding would lose quality for no gain",
        )
    if source < 64:
        # Keep roughly the source rate rather than upscaling, which would waste
        # space without recovering anything.
        chosen = min(_floor_to_valid(source), max_bitrate_kbps)
        return Target(
            chosen,
            1,
            f"mono source at {source}k -> {chosen}k mono, matching the source",
        )
    chosen = min(64, max_bitrate_kbps)
    return Target(
        chosen,
        1,
        f"mono source at {source}k -> {chosen}k mono",
    )


def _floor_to_valid(kbps: int) -> int:
    """Snap down to a supported bitrate, never below the lowest one.

    Flooring rather than rounding to nearest avoids ever encoding *above* the
    source rate, which costs space without improving anything.
    """

    allowed = [value for value in VALID_BITRATES if value <= kbps]
    return allowed[-1] if allowed else VALID_BITRATES[0]


def nearest_valid(kbps: int) -> int:
    """Snap to the closest supported bitrate, preferring the lower on a tie."""

    return min(VALID_BITRATES, key=lambda value: (abs(value - kbps), value))


# --------------------------------------------------------------------------- #
# Dramatic-audio detection (a suggestion for triage, never a decision)
# --------------------------------------------------------------------------- #


def _normalize(text: str) -> str:
    """Strip punctuation and collapse whitespace before phrase matching."""

    cleaned = re.sub(r"[_\W]+", " ", text.casefold(), flags=re.UNICODE)
    return " ".join(cleaned.split())


def detect_dramatic(info: AudioInfo) -> DramaticMatch | None:
    """Look for signs that *info* is a dramatised production.

    Used only to pre-select a disposition in triage, so the user confirms rather
    than types.  The looser variant in the GUI matched the bare words "dramatic"
    and "graphic", which caught ordinary books with "graphic" in the title.
    """

    candidates: list[tuple[str, str]] = [
        (key, value) for key, value in info.metadata.items() if value
    ]
    candidates.append(("filename", info.path.name))

    phrases = [(phrase, _normalize(phrase)) for phrase in DRAMATIC_PHRASES]
    for field_name, raw in candidates:
        normalized = _normalize(raw)
        compact = normalized.replace(" ", "")
        for original, phrase in phrases:
            if phrase in normalized or phrase.replace(" ", "") in compact:
                return DramaticMatch(field=field_name, value=raw, phrase=original)
    return None


def suggest_disposition(info: AudioInfo) -> tuple[Disposition, str]:
    """Propose a disposition for a freshly scanned book, with a reason.

    This only ever pre-selects a choice in the triage UI.  Nothing acts on a
    suggestion until the user accepts it and it is written to the file.
    """

    dramatic = detect_dramatic(info)
    if dramatic is not None:
        return (
            Disposition.DRAMA,
            f"{dramatic.phrase!r} found in {dramatic.field}",
        )

    is_mp4 = info.path.suffix.casefold() in {".m4b", ".m4a"}
    downcode = decide(info, Disposition.DOWNCODE)

    if downcode.declined:
        if is_mp4:
            return Disposition.AS_IS, f"already m4b; {downcode.rationale}"
        return Disposition.REMUX, f"not worth re-encoding; {downcode.rationale}"

    if not is_mp4:
        return Disposition.DOWNCODE, downcode.rationale
    if downcode.bitrate_kbps is not None and downcode.bitrate_kbps >= info.normalized_bitrate_kbps:
        return Disposition.AS_IS, "already at or below the target bitrate"
    return Disposition.DOWNCODE, downcode.rationale
