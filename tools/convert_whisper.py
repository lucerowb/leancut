"""Convert a fine-tuned Whisper model from Hugging Face (Transformers format) into a CTranslate2 folder that
LeanCut (faster-whisper) can load as a custom Whisper model.

Needs packages LeanCut itself doesn't: run it in a separate virtual environment.
    python -m venv convert-venv
    convert-venv/bin/pip install ctranslate2 "transformers[torch]<5"
    convert-venv/bin/python tools/convert_whisper.py kiranpantha/whisper-large-v3-turbo-nepali ~/whisper-nepali

Then put the output folder in Settings › AI models › Custom Whisper model. Check the model's licence on its
Hugging Face page (and its base model's) before you use it for commercial work.
"""
import argparse
import sys
from pathlib import Path

QUANTIZATIONS = ["float16", "int8_float16", "int8", "bfloat16", "float32"]


def convert(repo, out, quantization="float16", processor=None, force=False):
    from ctranslate2.converters import TransformersConverter
    from transformers import AutoProcessor

    out = Path(out).expanduser()
    TransformersConverter(repo, load_as_float16=quantization in ("float16", "int8_float16")).convert(
        str(out), quantization=quantization, force=force)
    # faster-whisper reads tokenizer.json and preprocessor_config.json (n_mels: 128 for v3 models) from the folder;
    # many fine-tunes ship only slow-tokenizer files, so write both from the processor instead of copying.
    AutoProcessor.from_pretrained(processor or repo).save_pretrained(str(out))
    missing = [f for f in ("model.bin", "tokenizer.json", "preprocessor_config.json") if not (out / f).exists()]
    if missing:
        raise RuntimeError(f"Conversion incomplete, missing {', '.join(missing)} in {out}")
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("repo", help="Hugging Face repo id or local folder of a Transformers Whisper model")
    p.add_argument("out", help="output folder for the CTranslate2 model")
    p.add_argument("--quantization", default="float16", choices=QUANTIZATIONS,
                   help="weight type on disk (default float16; LeanCut still runs it as int8 on CPU)")
    p.add_argument("--processor", help="repo to take tokenizer/feature settings from when the model lacks them "
                                       "(usually its base model, e.g. openai/whisper-large-v3-turbo)")
    p.add_argument("--force", action="store_true", help="overwrite an existing output folder")
    a = p.parse_args(argv)
    print(f"Converted to {convert(a.repo, a.out, a.quantization, a.processor, a.force)}")


if __name__ == "__main__":
    sys.exit(main())
