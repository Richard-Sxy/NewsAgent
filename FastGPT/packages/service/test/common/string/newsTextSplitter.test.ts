import { describe, expect, it } from 'vitest';
import { newsText2Chunks, parseNewsText } from '../../../common/string/newsTextSplitter';
import { splitText2Chunks } from '../../../common/string/textSplitter';

describe('newsText2Chunks', () => {
  it('parses news metadata with Chinese and ASCII colons', () => {
    const result = parseNewsText(`# 新闻标题

来源地址：https://news.qq.com/example
作者: 测试作者
发布时间：2026-08-21 10:00:00

这是新闻导语。
这是新闻正文。`);

    expect(result).toEqual({
      title: '新闻标题',
      source: 'https://news.qq.com/example',
      author: '测试作者',
      publishTime: '2026-08-21 10:00:00',
      lead: '这是新闻导语。',
      paragraphs: ['这是新闻导语。', '这是新闻正文。']
    });
  });

  it('creates an article chunk and contextual paragraph chunks', () => {
    const text = `# 火箭回收试验成功

来源地址：https://news.qq.com/example
作者：记者甲
发布时间：2026-08-21 10:00:00

某型火箭今天完成回收试验。
一、试验过程
火箭按计划升空并返回着陆场。
现场团队确认设备状态正常。
二、后续计划
团队将继续开展重复使用测试。`;

    const result = newsText2Chunks({ text, chunkSize: 120, overlapRatio: 0 });

    expect(result.chars).toBe(text.length);
    expect(result.chunks.length).toBeGreaterThanOrEqual(3);
    expect(result.chunks[0]).toContain('文章标题：火箭回收试验成功');
    expect(result.chunks[0]).toContain('新闻导语：某型火箭今天完成回收试验。');
    expect(result.chunks.some((chunk) => chunk.includes('段落主题：试验过程'))).toBe(true);
    expect(result.chunks.some((chunk) => chunk.includes('段落主题：后续计划'))).toBe(true);
    expect(result.chunks.every((chunk) => chunk.length <= 120)).toBe(true);
  });

  it('selects the lead after cleaning noise and skips headings', () => {
    const text = `# 新闻标题

来源地址：https://news.qq.com/example
发布时间：2026-08-21 10:00:00

<!-- NO_READ_BEGIN -->
相关阅读：不应进入正文。
<!-- NO_READ_END -->
图源：视觉中国
编辑：测试编辑
一、事件进展
这是清洗后的第一段正文，应当成为导语。
这是第二段正文。`;

    const parsed = parseNewsText(text);
    const result = newsText2Chunks({ text, chunkSize: 160, overlapRatio: 0 });

    expect(parsed.lead).toBe('这是清洗后的第一段正文，应当成为导语。');
    expect(parsed.paragraphs).toEqual([
      '一、事件进展',
      '这是清洗后的第一段正文，应当成为导语。',
      '这是第二段正文。'
    ]);
    expect(result.chunks[0]).toContain('新闻导语：这是清洗后的第一段正文，应当成为导语。');
    expect(result.chunks.join('\n')).not.toContain('视觉中国');
    expect(result.chunks.join('\n')).not.toContain('测试编辑');
    expect(result.chunks.join('\n')).not.toContain('相关阅读');
  });

  it('splits oversized paragraphs while preserving news context', () => {
    const text = `# 长新闻

来源地址：https://news.qq.com/example

这是导语。
${'这是需要切分的长段落。'.repeat(30)}`;
    const result = newsText2Chunks({ text, chunkSize: 80, overlapRatio: 0 });

    expect(result.chunks.length).toBeGreaterThan(2);
    expect(result.chunks.slice(1).every((chunk) => chunk.includes('文章标题：长新闻'))).toBe(true);
    expect(result.chunks.every((chunk) => chunk.length <= 80)).toBe(true);
  });

  it('falls back to the common splitter for non-news text', () => {
    const props = {
      text: '第一段新闻内容。\n\n第二段新闻内容。',
      chunkSize: 10,
      overlapRatio: 0
    };
    const result = newsText2Chunks(props);
    const fallbackResult = splitText2Chunks(props);

    expect(result).toEqual(fallbackResult);
  });

  it('handles empty text and rejects an invalid chunk size', () => {
    expect(newsText2Chunks({ text: '   ', chunkSize: 100 })).toEqual({
      chunks: [],
      chars: 0
    });
    expect(() => newsText2Chunks({ text: '内容', chunkSize: 0 })).toThrow(
      'chunkSize must be greater than 0'
    );
  });
});
