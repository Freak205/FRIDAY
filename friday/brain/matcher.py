"""L2 — intent matching by embedding similarity.

Every skill ships example phrasings. Those, plus anything you've taught FRIDAY at
runtime, are embedded once and cached. An utterance is embedded and cosine-matched
against the corpus.

This is what gives flexibility without a language model: "make it louder",
"crank it up" and "i can't hear this" all land on system.volume.up without a rule
being written for any of them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np

from friday import paths, store
from friday.brain.rejects import REJECT_PHRASES, SENTINEL
from friday.config import CFG
from friday.log import get
from friday.registry import REGISTRY

log = get(__name__)


@dataclass(slots=True)
class Match:
    skill: str
    score: float
    matched_example: str


class Matcher:
    def __init__(self) -> None:
        self._model = None
        self._vectors: np.ndarray | None = None
        self._corpus: list[tuple[str, str]] = []  # (phrase, skill_name)
        self._ready = False

    # -- model ---------------------------------------------------------------

    def _load_model(self):
        if self._model is None:
            from fastembed import TextEmbedding

            paths.ensure()
            log.info("loading embedding model %s", CFG.brain.embedding_model)
            self._model = TextEmbedding(
                model_name=CFG.brain.embedding_model,
                cache_dir=str(paths.MODELS),
            )
        return self._model

    def _embed(self, texts: list[str]) -> np.ndarray:
        model = self._load_model()
        vecs = np.array(list(model.embed(texts)), dtype=np.float32)
        # Cosine similarity via dot product requires unit vectors.
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vecs / norms

    # -- corpus --------------------------------------------------------------

    def _build_corpus(self) -> list[tuple[str, str]]:
        corpus: list[tuple[str, str]] = []

        for skill in REGISTRY.all():
            for example in skill.examples:
                corpus.append((example.lower().strip(), skill.name))
            # The description is a weak but useful extra anchor.
            corpus.append((skill.description.lower().strip(), skill.name))

        # Negative anchors, so out-of-domain utterances lose to the sentinel
        # rather than to whichever real skill is least dissimilar.
        corpus.extend((p.lower().strip(), SENTINEL) for p in REJECT_PHRASES)

        # Phrasings you've taught it are first-class corpus entries.
        try:
            rows = store.conn().execute(
                "SELECT text, intent FROM phrasings"
            ).fetchall()
            corpus.extend((r["text"].lower().strip(), r["intent"]) for r in rows)
        except Exception:
            log.exception("could not load taught phrasings")

        return corpus

    def _fingerprint(self, corpus: list[tuple[str, str]]) -> str:
        blob = json.dumps(sorted(corpus), ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    # -- lifecycle -----------------------------------------------------------

    def build(self, force: bool = False) -> None:
        """Embed the corpus, reusing the on-disk cache when nothing changed."""
        corpus = self._build_corpus()
        if not corpus:
            log.warning("matcher: empty corpus, no skills registered?")
            return

        fingerprint = self._fingerprint(corpus)
        cache_file = paths.CACHE / f"intents-{fingerprint}.npz"

        if cache_file.exists() and not force:
            data = np.load(cache_file, allow_pickle=True)
            self._vectors = data["vectors"]
            self._corpus = [tuple(x) for x in data["corpus"]]
            self._ready = True
            log.info("matcher: %d phrases from cache", len(self._corpus))
            return

        log.info("matcher: embedding %d phrases (first run takes a moment)", len(corpus))
        vectors = self._embed([p for p, _ in corpus])

        paths.ensure()
        # Drop stale caches so data/cache doesn't accumulate.
        for old in paths.CACHE.glob("intents-*.npz"):
            old.unlink(missing_ok=True)
        np.savez_compressed(
            cache_file, vectors=vectors, corpus=np.array(corpus, dtype=object)
        )

        self._vectors = vectors
        self._corpus = corpus
        self._ready = True
        log.info("matcher: ready, %d phrases", len(corpus))

    @property
    def ready(self) -> bool:
        return self._ready

    # -- matching ------------------------------------------------------------

    def match(self, utterance: str, top_k: int = 5) -> list[Match]:
        """Rank skills by similarity. Best first."""
        if not self._ready or self._vectors is None:
            self.build()
        if self._vectors is None or not utterance.strip():
            return []

        query = self._embed([utterance])[0]
        scores = self._vectors @ query  # both unit-normalised -> cosine

        # Keep only the best-scoring phrase per skill.
        best: dict[str, tuple[float, str]] = {}
        for idx, score in enumerate(scores):
            phrase, skill_name = self._corpus[idx]
            current = best.get(skill_name)
            if current is None or score > current[0]:
                best[skill_name] = (float(score), phrase)

        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])[:top_k]
        return [
            Match(skill=name, score=score, matched_example=phrase)
            for name, (score, phrase) in ranked
        ]

    def teach(self, utterance: str, skill_name: str) -> None:
        """Permanently associate a phrasing with a skill, then rebuild."""
        if REGISTRY.get(skill_name) is None:
            raise KeyError(f"unknown skill: {skill_name}")

        c = store.conn()
        c.execute(
            "INSERT INTO phrasings (at, intent, text, source) VALUES (?,?,?,?)",
            (store.now(), skill_name, utterance.lower().strip(), "taught"),
        )
        c.commit()
        log.info("taught: %r -> %s", utterance, skill_name)
        self.build(force=True)


MATCHER = Matcher()


def embed(texts: list[str]) -> np.ndarray:
    """Unit-normalised embeddings, sharing the matcher's already-loaded model.

    Memory and document search reuse this rather than loading a second copy —
    the model is ~130 MB and RAM is the binding constraint on this machine.
    """
    return MATCHER._embed(texts)
