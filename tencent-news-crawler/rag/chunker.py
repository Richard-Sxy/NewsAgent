
"""按照文本进行切分"""
def chunk_text(
    text: str,
    chunk_size: int = 800,
    overlap: int = 100,
) -> list[str]:
    # 做一个简单的参数校验
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than 0")
    if overlap < 0:
        raise ValueError("overlap must be greater than or equal to 0")
    if overlap >= chunk_size:
        raise ValueError("overlap must be less than chunk_size")
    
    chunks = []
    start = 0

    while start < len(text):
        end = start + chunk_size

        chunk = text[start:end]

        chunks.append(chunk)

        start += chunk_size - overlap

    return chunks

# 这边是练习手搓代码
def chunk_text_second(
    text: str,
    chunk_size: 400,
    overlap: 100,
) -> list[str]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must larger than 0")
    if overlap < 0:
        raise ValueError("overlap must be larger than 0")
    if overlap >= chunk_size:
        raise ValueError("overlap must smaller than chunk_size")

    chunks = []
    start = 0

    while start < len(text):
        end = start + overlap
        chunk = text[start:end]
        chunks.append(chunk)
        start += chunk_size - overlap

    return chunks