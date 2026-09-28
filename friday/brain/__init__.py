"""The brain: turn an utterance into a skill call.

Five layers, none of which require a cloud API:

  L1 normalize   clean up the raw transcript
  L2 match       embedding similarity against every skill's example phrasings
  L3 extract     pull arguments out of the utterance and resolve them
  L4 plan        (P2) multi-step task graphs
  L5 escape      (P3) local 3B model for utterances L2 can't place
"""

from friday.brain.engine import BRAIN, Understanding

__all__ = ["BRAIN", "Understanding"]
