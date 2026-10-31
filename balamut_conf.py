"""balamut's own dogfood profile: mutating balamut with its own suite.

Everything runs on the defaults; the measurement worker scrubs the coverage environment before
each test body, so the daemon and e2e tests (which spawn balamut themselves) run nested cleanly.
"""

from balamut.config import init

init()
