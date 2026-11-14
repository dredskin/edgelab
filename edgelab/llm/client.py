"""LLM reading layer.

Rules baked in (from the LLM-trainer panelist):
  1. Contamination guard: any document dated on/before the model's training cutoff is refused,
     because the model may already "know" what happened next.
  2. Frozen model + prompt version + temperature 0, and every answer cached to disk,
     so results are reproducible and re-runs cost nothing.
  3. The LLM only extracts features (scores). A statistical layer decides what predicts returns.
"""
import hashlib
import json
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd


class ContaminationError(Exception):
    pass


PROMPTS = {
    "earnings_v1": """You are a skeptical equity analyst reading an earnings release for a small company
that few analysts cover. Judge it only on the text below.

Return ONLY a JSON object:
{{"surprise": <-1.0 to 1.0, how much better (+) or worse (-) than the company's own prior trajectory and guidance>,
  "evasion": <0.0 to 1.0, how much the release dodges, omits, or softens key numbers vs. a plain disclosure>,
  "confidence": <0.0 to 1.0>,
  "reason": "<one sentence>"}}

Do not reward promotional tone. Missing numbers raise evasion.

TEXT:
{text}""",
    "headline_v1": """You are judging whether a news headline contains NEW, material, verifiable information
about a company's cash flows, or is mostly keyword hype (AI, partnership, breakthrough, etc.).

Return ONLY a JSON object:
{{"substance": <0.0 = pure hype/no new information, 1.0 = clearly material new facts>,
  "reason": "<one sentence>"}}

HEADLINE AND SNIPPET:
{text}""",
    # v2: multilingual. Documents may be in any language (Japanese, Chinese, Korean, German, ...).
    "earnings_v2": """You are a skeptical equity analyst reading an earnings release or results filing for a small
company that few analysts cover. The text may be in ANY language; read it in the original language, do not
assume an English translation exists, and judge it only on the text below.

Return ONLY a JSON object (English values):
{{"language": "<ISO 639-1 code of the document>",
  "surprise": <-1.0 to 1.0, how much better (+) or worse (-) than the company's own prior trajectory and guidance>,
  "evasion": <0.0 to 1.0, how much the release dodges, omits, or softens key numbers vs. a plain disclosure>,
  "confidence": <0.0 to 1.0, lower if the language, accounting standard or units are unclear>,
  "reason": "<one sentence in English>"}}

Do not reward promotional tone. Missing numbers raise evasion. Watch units (e.g. Japanese 百万円, Chinese 万元,
Korean 억원) and fiscal-year conventions.

TEXT:
{text}""",
    "headline_v2": """You are judging whether a news headline contains NEW, material, verifiable information
about a company's cash flows, or is mostly keyword hype (AI, partnership, breakthrough, etc.).
The headline may be in ANY language; judge it in the original.

Return ONLY a JSON object (English values):
{{"language": "<ISO 639-1 code>",
  "substance": <0.0 = pure hype/no new information, 1.0 = clearly material new facts>,
  "reason": "<one sentence in English>"}}

HEADLINE AND SNIPPET:
{text}""",
    "announce_v2": """You are sorting a corporate announcement (any language) into a category for a research system.
Read it in the original language.

Return ONLY a JSON object (English values):
{{"language": "<ISO 639-1 code>",
  "kind": "<one of: earnings | insider_buy | buyback | large_stake | other>",
  "exchange": "<exchange label if stated, e.g. LSE, HKEX, TSX, TSXV, ASX, XETRA; else empty>",
  "symbol": "<ticker/stock code if stated; else empty>",
  "n_insiders": <for insider_buy: number of distinct directors/officers who BOUGHT shares; else 0>,
  "first_purchase": <true only if the text says this is the person's first purchase; else false>}}

Definitions: earnings = results or guidance/forecast changes. insider_buy = directors/officers/PDMRs buying
shares on the open market (not option exercises, grants or sales). buyback = the company decides to
repurchase its own shares (not progress updates). large_stake = an outside holder crosses or raises a
disclosed stake (e.g. 5%). Everything else = other.

ANNOUNCEMENT:
{text}""",
    "buyback_v2": """Read this share-buyback announcement (any language) and extract its size.

Return ONLY a JSON object:
{{"pct_of_shares": <maximum shares to be repurchased as a percentage of shares outstanding, e.g. 3.2; 0 if not stated>,
  "confidence": <0.0 to 1.0>}}

ANNOUNCEMENT:
{text}""",
}


class LLMClient:
    def __init__(self, cfg: dict):
        c = cfg["llm"]
        self.model = c["model"]
        self.temperature = c.get("temperature", 0)
        self.version = c.get("prompt_version", "v1")
        self.cutoff = pd.Timestamp(c["training_cutoff"])
        self.cache = Path(cfg.get("_root", ".")) / c.get("cache_dir", ".llm_cache")
        self.cache.mkdir(exist_ok=True)
        self.refused = 0
        self._client = None

    def guard(self, doc_date):
        if pd.Timestamp(doc_date) <= self.cutoff:
            self.refused += 1
            raise ContaminationError(f"document {doc_date} is before model cutoff {self.cutoff.date()}")

    def _ask(self, prompt_key: str, text: str) -> dict:
        name = f"{prompt_key}_{self.version}"
        if name not in PROMPTS:                    # prompts introduced later than the configured version
            name = sorted(k for k in PROMPTS if k.startswith(prompt_key + "_"))[0]
        prompt = PROMPTS[name].format(text=text)
        key = hashlib.sha256(f"{self.model}|{self.temperature}|{prompt}".encode()).hexdigest()
        f = self.cache / f"{key}.json"
        if f.exists():
            return json.loads(f.read_text())
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        msg = self._client.messages.create(model=self.model, max_tokens=300,
                                           temperature=self.temperature,
                                           messages=[{"role": "user", "content": prompt}])
        raw = msg.content[0].text
        m = re.search(r"\{.*\}", raw, re.S)
        out = json.loads(m.group(0)) if m else {}
        f.write_text(json.dumps(out))
        return out

    def score_earnings(self, event) -> dict:
        self.guard(event.date)
        out = self._ask("earnings", event.payload["text"])
        return {"surprise": float(out.get("surprise", 0)), "evasion": float(out.get("evasion", 0)),
                "confidence": float(out.get("confidence", 0)), "language": out.get("language", "")}

    def score_headline(self, event) -> dict:
        self.guard(event.date)
        out = self._ask("headline", event.payload["text"])
        return {"substance": float(out.get("substance", 1.0))}


    def classify_announcement(self, text: str, doc_date) -> dict:
        self.guard(doc_date)
        return self._ask("announce", text[:6000])

    def buyback_size(self, event) -> dict:
        self.guard(event.date)
        out = self._ask("buyback", event.payload.get("text", "")[:8000])
        return {"pct_of_shares": float(out.get("pct_of_shares") or 0), "confidence": float(out.get("confidence") or 0)}


class MockLLM(LLMClient):
    """Synthetic-world stand-in: reads the hidden truth and adds noise, like an imperfect analyst."""

    def __init__(self, cutoff, noise=0.3, seed=7):
        self.cutoff = pd.Timestamp(cutoff)
        self.noise = noise
        self.rng = np.random.default_rng(seed)
        self.refused = 0

    def score_earnings(self, event):
        self.guard(event.date)
        s = event.payload["_true_surprise"] + self.rng.normal(0, self.noise)
        return {"surprise": float(np.clip(s, -1, 1)), "evasion": float(self.rng.uniform(0, 0.3)),
                "confidence": 0.7}

    def score_headline(self, event):
        self.guard(event.date)
        s = event.payload["_true_substance"] + self.rng.normal(0, self.noise / 2)
        return {"substance": float(np.clip(s, 0, 1))}
