"""Structured chunking tests (tools/retrieval/chunking.py).

Covers: paragraph boundaries, sentence-boundary splitting of oversized
paragraphs, whole-sentence overlap, and tiny-tail merging.
"""
from __future__ import annotations

from tools.retrieval.chunking import smart_chunks, split_sentences


def test_empty_text_returns_nothing():
    assert smart_chunks("") == []
    assert smart_chunks("   \n\n ") == []


def test_short_text_single_chunk_untouched():
    text = "短段落。保持完整。"
    assert smart_chunks(text, size=900) == [text]


def test_paragraphs_packed_without_splitting_sentences():
    p1 = "第一段讲的是图神经网络的基本原理。" * 5
    p2 = "第二段讨论注意力机制。"
    chunks = smart_chunks(f"{p1}\n\n{p2}", size=900)
    assert len(chunks) == 1
    assert "第二段讨论注意力机制。" in chunks[0]


def test_long_paragraph_splits_on_sentence_boundaries():
    sents = [f"这是第{i}个关于模型结构的完整句子。" for i in range(20)]
    text = "".join(sents)
    chunks = smart_chunks(text, size=120)
    assert len(chunks) > 1
    for c in chunks:
        # every chunk ends at a sentence boundary (or is the overlap-prefixed tail)
        assert c.rstrip().endswith("。")
        # no sentence is cut in the middle: each chunk fully contains its sentences
        body = c
        for s in sents:
            if s[:-1] in body:  # sentence minus its terminator appears => whole sentence present
                assert s in body


def test_overlap_carries_whole_sentences():
    sents = [f"句子编号{i}讲述一个独立观点。" for i in range(12)]
    chunks = smart_chunks("".join(sents), size=100, overlap_sentences=1)
    assert len(chunks) > 1
    for i in range(1, len(chunks)):
        prev_tail = split_sentences(chunks[i - 1])[-1]
        assert chunks[i].startswith(prev_tail)


def test_tiny_tail_merged_into_previous():
    body = "主要内容段落。" * 30  # ~ long enough for several chunks
    text = body + "\n\n尾注。"  # tiny trailing paragraph
    chunks = smart_chunks(text, size=200)
    assert chunks
    assert all(len(c) >= 4 for c in chunks)
    # the tiny tail was merged, not left as its own chunk
    assert "尾注。" in chunks[-1]
    assert chunks[-1] != "尾注。"


def test_cjk_and_latin_sentences_both_split():
    text = "Graph neural networks propagate messages. 图神经网络传递消息。Attention helps. 注意力有帮助。"
    sents = split_sentences(text)
    assert "Graph neural networks propagate messages." in sents[0]
    assert any("图神经网络传递消息。" in s for s in sents)
