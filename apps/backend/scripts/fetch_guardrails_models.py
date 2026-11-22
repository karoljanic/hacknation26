"""Pre-fetch the Guardrails DetectJailbreak models at pinned revisions.

Usage: HF_HOME=<cache dir> python fetch_guardrails_models.py

``guardrails_ai.detect_jailbreak`` loads its models by name (the "main"
branch). To keep builds reproducible and safe from upstream changes, each
model is downloaded at a fixed commit, and the cache's ``refs/main`` is
pointed at that commit, so the app (run with ``HF_HUB_OFFLINE=1``) loads
exactly these files and never contacts the Hub. Only safetensors weights and
tokenizer/config files are fetched (no pickle weights).

Licences: bert-base-cased and all-MiniLM-L6-v2 Apache-2.0, ftrobertallm MIT;
GuardrailsAI/prompt-saturation-attack-detector states no licence on its model
page (the guardrails-ai-detect-jailbreak package that uses it is MIT).
"""

from pathlib import Path

from huggingface_hub import constants, snapshot_download

MODELS = {
    "GuardrailsAI/prompt-saturation-attack-detector": "6e8dbe4acfa5d699a1512f07885874260e8ae4ca",
    "google-bert/bert-base-cased": "cd5ef92a9fb2f889e972770a36d4ed042daf221e",  # tokenizer only
    "sentence-transformers/all-MiniLM-L6-v2": "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
    "zhx123/ftrobertallm": "edf8a6fa8e51a5e06b0139cc5c7d3358eeb79cf3",
}
TOKENIZER_FILES = ["config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
                   "vocab.txt", "vocab.json", "merges.txt"]


def main() -> None:
    cache = Path(constants.HF_HUB_CACHE)
    for repo, revision in MODELS.items():
        patterns = TOKENIZER_FILES if repo == "google-bert/bert-base-cased" else [*TOKENIZER_FILES, "model.safetensors"]
        try:
            snapshot_download(repo, revision=revision, allow_patterns=patterns)
        except Exception as exc:
            # The image still builds (e.g. behind a proxy blocking huggingface.co);
            # the API then screens with JEV only and logs it.
            print(f"WARNING: cannot download {repo} ({exc}); the Guardrails detector will be unavailable")
            return
        ref = cache / f"models--{repo.replace('/', '--')}" / "refs" / "main"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text(revision)
        print(f"ok  {repo}@{revision[:12]}")


if __name__ == "__main__":
    main()
