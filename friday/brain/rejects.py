"""Out-of-domain rejection corpus.

Embedding similarity always returns *something*. Without negative anchors, an
utterance FRIDAY has no business acting on ("write me a poem") lands on whichever
skill happens to be least dissimilar — and then gets executed.

These phrases are embedded alongside the real skills under a sentinel intent. If
the winning match is one of them, the brain declines instead of guessing. This is
the cheapest available defence against confidently doing the wrong thing.
"""

SENTINEL = "__none__"

REJECT_PHRASES: list[str] = [
    # open-ended generation — squarely out of scope for a local intent engine
    "write me a poem about the sea",
    "write an essay about history",
    "compose a song",
    "tell me a story",
    "write me a long email to my boss",
    "draft a blog post",
    "generate some code for me",
    "write a python script that sorts a list",
    # general knowledge questions
    "what is the capital of peru",
    "who was the first president",
    "explain quantum physics to me",
    "how does photosynthesis work",
    "what year did the war end",
    "translate this into french",
    "what does this word mean",
    "how do you spell restaurant",
    # opinion, advice, conversation
    "what do you think about this",
    "give me some advice",
    "tell me a joke",
    "how are you feeling today",
    "what should i have for dinner",
    "do you like music",
    "convince me to go running",
    # maths and reasoning
    "what is two hundred times fifteen",
    "solve this equation for me",
    "summarize this article",
    # ambiguous fragments that shouldn't trigger anything
    "hmm",
    "never mind",
    "wait",
    "hold on",
    "actually forget it",
    "no not that",
]
