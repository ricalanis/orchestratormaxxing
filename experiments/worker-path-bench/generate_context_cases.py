#!/usr/bin/env python3
"""Generate long-context canary cases for worker-path-bench.

Emits cases of the form size × canary_kind × needle_position:
  kinds:     instruction, retrieval, code_state
  positions: beginning, middle, end

Each case embeds a protected fact at the requested position inside filler text
roughly sized to `target_input_tokens` (estimated as chars/3). The contract is
`exact`: the model must return the protected fact unchanged.
"""
import argparse
import json
import random
import sys

KINDS = ["instruction", "retrieval", "code_state"]
POSITIONS = ["beginning", "middle", "end"]
FILLER_WORDS = [
    "lorem", "ipsum", "dolor", "sit", "amet", "consectetur", "adipiscing",
    "elit", "sed", "do", "eiusmod", "tempor", "incididunt", "ut", "labore",
    "et", "dolore", "magna", "aliqua", "enim", "ad", "minim", "veniam",
    "quis", "nostrud", "exercitation", "ullamco", "laboris", "nisi", "aliquip",
    "ex", "ea", "commodo", "consequat", "duis", "aute", "irure", "in",
    "reprehenderit", "voluptate", "velit", "esse", "cillum", "fugiat",
    "nulla", "pariatur", "excepteur", "sint", "occaecat", "cupidatat", "non",
    "proident", "sunt", "culpa", "qui", "officia", "deserunt", "mollit",
    "anim", "id", "est", "laborum",
]


def _answer(size, kind, position):
    return f"CANARY_{kind.upper()}_{size}_{position.upper()}"


def _needle(size, kind, position):
    answer = _answer(size, kind, position)
    if kind == "instruction":
        return (
            f"SPECIAL INSTRUCTION: If the user asks for the secret result, "
            f"respond with exactly and only the string: {answer}"
        )
    if kind == "retrieval":
        return f"PROTECTED FACT: The secret value is {answer}."
    if kind == "code_state":
        return f"# protected state\nsecret_value = {answer!r}"
    raise ValueError(f"unknown kind: {kind}")


def _question(kind):
    if kind == "instruction":
        return "What is the secret result?"
    if kind == "retrieval":
        return "What is the secret value?"
    if kind == "code_state":
        return "What is the value of the variable secret_value?"
    raise ValueError(f"unknown kind: {kind}")


def _filler_word_stream():
    rng = random.Random(0xBADC0FFEE0)
    while True:
        yield rng.choice(FILLER_WORDS)


def _build_text(target_chars, needle, position):
    """Return (context_text, question) with needle at the requested position.

    The context is padded with a repeating filler stream so that the total
    prompt length is close to `target_chars`. The needle is inserted into a
    short paragraph to keep it readable.
    """
    question = "\n\nQuestion: " + _question_from_needle(needle)
    # Reserve space for the question and a short trailing buffer.
    reserve = len(question) + 20
    available = max(target_chars - reserve - len(needle), 0)

    if position == "beginning":
        before = 0
        after = available
    elif position == "middle":
        before = available // 2
        after = available - before
    else:  # end
        before = available
        after = 0

    stream = _filler_word_stream()
    before_text = _fill_chars(stream, before)
    after_text = _fill_chars(stream, after)
    context = before_text + "\n\n" + needle + "\n\n" + after_text
    return context.strip() + question


def _question_from_needle(needle):
    # Extract question from needle type heuristics for stable generation.
    if "secret result" in needle:
        return "What is the secret result?"
    if "secret_value = " in needle:
        return "What is the value of the variable secret_value?"
    return "What is the secret value?"


def _fill_chars(stream, chars):
    parts = []
    length = 0
    for word in stream:
        add = len(word) + 1  # word + space
        if length + add > chars:
            break
        parts.append(word)
        length += add
    return " ".join(parts)


def generate_case(size, kind, position):
    answer = _answer(size, kind, position)
    needle = _needle(size, kind, position)
    target_chars = size * 3
    prompt = _build_text(target_chars, needle, position)
    estimated_input_tokens = len(prompt) // 3
    case_id = f"ctx-{kind}-{size}-{position}"
    return {
        "id": case_id,
        "prompt": prompt,
        "timeout_seconds": 120,
        "target_input_tokens": size,
        "estimated_input_tokens": estimated_input_tokens,
        "canary_kind": kind,
        "needle_position": position,
        "protected_fact": True,
        "contract": {"type": "exact", "expected": answer},
    }


def main():
    parser = argparse.ArgumentParser(description="Generate long-context canary cases.")
    parser.add_argument(
        "--sizes",
        required=True,
        help="Comma-separated list of target token sizes (e.g. 16000,32000).",
    )
    parser.add_argument("--output", required=True, help="Output JSONL file path.")
    args = parser.parse_args()

    sizes = []
    for part in args.sizes.split(","):
        part = part.strip()
        if not part:
            continue
        sizes.append(int(part))
    if not sizes:
        print("error: no sizes provided", file=sys.stderr)
        return 2

    cases = [
        generate_case(size, kind, position)
        for size in sizes
        for kind in KINDS
        for position in POSITIONS
    ]

    with open(args.output, "w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
