import pytest
from rag.chunker import chunk_text

def test_chunk_empty_text():
    """空文本不产生任何分块"""
    assert chunk_text("") == []

def test_chunk_text_shorter_than_chunk_size():
    """短于 chunk_size 的文本只产生一个分块。"""
    assert chunk_text(
        text="腾讯新闻",
        chunk_size=10,
        overlap=2,
    ) == ["腾讯新闻"]

def test_chunk_text_without_overlap():
    """overlap 为 0 时，分块之间没有重叠"""
    chunks = chunk_text(
        text="abcdefgh",
        chunk_size=3,
        overlap=0,
    )

    assert chunks == ["abc", "def", "gh"]

def test_chunk_text_with_overlap():
    """相邻分块应该保留指定长度的重叠文本"""
    chunks = chunk_text(
        text="abcdefgh",
        chunk_size=4,
        overlap=1,
    )
    
    assert chunks == ["abcd", "defg", "gh"]
    assert chunks[0][-1:] == chunks[1][0:1]
    assert chunks[1][-1:] == chunks[2][:1]

@pytest.mark.parametrize(
    ("chunk_size", "overlap"),
    [
        (0, 0),
        (-1, 0),
        (5, -1),
        (5, 5),
        (5, 6),
    ],
)
def test_chunk_text_rejects_invalid_parameters(
    chunk_size: int,
    overlap: int,
):
    """非法参数必须立即报错，避免死循环"""
    with pytest.raises(ValueError):
        chunk_text(
            text="abcdefgh",
            chunk_size=chunk_size,
            overlap=overlap,
        )