"""Phase 3: BibTeX export for the personal paper library.

Pure functions: Paper -> @article{...} entry. No I/O, no API key. Easy to unit
-test. Used by the export endpoint and the chat/structured export buttons.
"""
from __future__ import annotations

import re
logger = None
