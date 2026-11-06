import json

import numpy as np

from council_diversity.concerns import chunk_paragraph, split_paragraphs, split_position
from council_diversity.judge import SCHEMA, blind, build_request, check_request, prefix_blocks
from council_diversity.novelty import auroc, best_match, bootstrap_share, floor


def _words(n: int, w: str = "word") -> str:
    return " ".join([w] * n)


def test_short_paragraphs_join_the_one_before():
    text = "\n\n".join([_words(50), _words(10), _words(45)])
    out = split_paragraphs(text)
    assert len(out) == 2
    assert len(out[0].split()) == 60


def test_short_first_paragraph_joins_the_next():
    out = split_paragraphs("\n\n".join([_words(5), _words(50)]))
    assert len(out) == 1 and len(out[0].split()) == 55


def test_questions_are_concerns_and_addressed_questions_keep_only_the_question():
    c, k = split_position(_words(50), ["q one?", {"for": "lawyer", "question": "q two?"}])
    assert k == ["para", "question", "question"]
    assert "lawyer" not in " ".join(c)


def test_chunk_variant_cuts_long_paragraphs_at_sentences():
    para = " ".join(f"Sentence {i} " + _words(12) + "." for i in range(30))
    chunks = chunk_paragraph(para)
    assert len(chunks) > 3
    assert all(len(x.split()) >= 40 for x in chunks)
    assert sum(len(x.split()) for x in chunks) == len(para.split())


def test_best_match_and_floor():
    a = np.eye(3)[:2]
    b = np.eye(3)[[1, 2]]
    s, i = best_match(a, b)
    assert list(s) == [0.0, 1.0] and i[1] == 0
    assert floor(list(range(101))) == 5.0


def test_bootstrap_share_point_and_interval():
    pt, lo, hi = bootstrap_share([(1, 10)] * 20)
    assert pt == 0.1 and lo == hi == 0.1


def test_auroc():
    assert auroc([3, 4], [False, True]) == 1.0
    assert auroc([1, 1], [False, True]) == 0.5


def test_blind_strips_names_and_formatting():
    t = blind("**As the Lawyer**, I think Claude Opus 4.6\n\n- a  point")
    for bad in ("Lawyer", "Claude", "Opus", "**", "\n", "  "):
        assert bad not in t


def test_judge_request_follows_the_api_rules():
    system, docs = prefix_blocks("c", "p")
    req = build_request(system, docs, "a", "b")
    check_request(req)
    s = json.dumps(SCHEMA)
    assert "$ref" not in s and "pattern" not in s
    assert req["messages"][0]["content"][-1]["text"].endswith("b")
