"""forge test suite.

Run with::

    python3 -m unittest discover -s tests -t .

Only the standard library is required.  forge is installed from distribution
packages with no virtual environment, so the tests must run the same way.
"""

import logging

# forge logs warnings for the cases these tests deliberately provoke - an
# unrecognised disposition, an unreadable file - which would otherwise bury the
# actual test results.
logging.getLogger("forge").setLevel(logging.CRITICAL)
logging.getLogger("forge").addHandler(logging.NullHandler())
