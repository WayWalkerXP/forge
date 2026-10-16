"""Putting text that forge did not write into a markup string.

Console markup is a formatting language, and tag values, file names and
Audiobookshelf's own fields are full of square brackets: "[2026] Book.mp3",
"Octaeteris Cycle #3".  Rendered unescaped, those raise ``MarkupError`` while
the widget is painting, which takes the interface down somewhere far away from
the data that caused it.

textual's own ``escape`` is not enough here: it only escapes a bracket that
looks like a tag (``[a-z#/@…]``), while the content parser rejects ``[2026]``
as well.  Escaping every opening bracket is the only thing that holds.
"""

from __future__ import annotations


def literal(value: object) -> str:
    """Return *value* as text that renders exactly as written."""

    text = str(value)
    # A backslash only matters to the parser when it precedes a bracket, so
    # they are left alone - except at the end, where there is no way to escape
    # one: it would escape the opening bracket of whatever markup follows.
    # Dropping a trailing backslash from a displayed tag value is a far smaller
    # loss than a crash while painting.
    return text.rstrip("\\").replace("[", "\\[")
