#!/usr/bin/env python3
"""Controlled model-preload command for the remote Mac transcription account."""

import argparse
import logging

from core import load_config
from transcribe_backend import preload_model

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    cfg = load_config(args.config)
    model_id = (cfg.get("transcription") or {}).get("model")
    if not model_id:
        parser.error("config.yaml is missing transcription.model")
    preload_model(model_id)
    logging.info("Preloaded transcription model %s.", model_id)


if __name__ == "__main__":
    main()
