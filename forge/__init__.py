"""forge - terminal audiobook conversion utility.

Attended runs triage books and record the decision as a disposition tag inside
the audio file itself.  Unattended runs read that tag and execute.  The absence
of a tag always means "skip", so nothing is ever processed because the tool
guessed.
"""

__version__ = "0.1.0"
