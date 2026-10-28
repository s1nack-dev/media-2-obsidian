#!/usr/bin/env python3
"""Controlled model-preload command for the remote Mac transcription account."""

import argparse
import logging
import os

from core import load_config

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--hf-home",
        help="Hugging Face cache dir; must match the bridge launcher's HF_HOME.",
    )
    args = parser.parse_args()
    if args.hf_home:
        # Must be set before huggingface_hub reads its constants.
        os.environ["HF_HOME"] = args.hf_home
    cfg = load_config(args.config)
    model_id = (cfg.get("transcription") or {}).get(
        "model", "mlx-community/parakeet-tdt-0.6b-v3"
    )
    from transcribe_backend import preload_model

    preload_model(model_id)
    logging.info("Preloaded transcription model %s.", model_id)


if __name__ == "__main__":
    main()
