"""docs/gpt/: the system prompt for a ChatGPT custom GPT fits its limit and
the example specs build."""

import json
from pathlib import Path

import pytest

from schemdraw_pfd import Flowsheet

GPT = Path(__file__).resolve().parent.parent / "docs" / "gpt"


def test_system_prompt_fits():
    assert len((GPT / "system_prompt.md").read_text("utf-8")) <= 5000


def test_chat_examples_fit():
    assert len((GPT / "chat_examples.md").read_text("utf-8")) <= 5000


def test_prompt_example_builds():
    text = (GPT / "system_prompt.md").read_text("utf-8")
    spec = text.split("```json\n", 1)[1].split("```", 1)[0]
    d = Flowsheet.from_dict(json.loads(spec)).build()
    assert not d.warnings


@pytest.mark.parametrize("name", ["example_distillation", "example_compressor"])
def test_example_builds(name):
    d = Flowsheet.from_json(GPT / f"{name}.json").build()
    assert d.routes
