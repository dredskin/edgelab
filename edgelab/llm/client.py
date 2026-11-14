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
        prompt = PROMPTS[f"{prompt_key}_{self.version}"].format(text=text)
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
                "confidence": float(out.get("confidence", 0))}

    def score_headline(self, event) -> dict:
        self.guard(event.date)
        out = self._ask("headline", event.payload["text"])
        return {"substance": float(out.get("substance", 1.0))}


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
