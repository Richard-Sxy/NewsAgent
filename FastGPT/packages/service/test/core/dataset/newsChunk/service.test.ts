import { describe, expect, it, vi } from 'vitest';
import {
  createNewsDatasetChunks,
  parseNewsAnalysis,
  type NewsAnalyzer
} from '../../../../core/dataset/newsChunk/service';

const newsText = `# 火箭回收试验成功

来源地址：https://news.qq.com/example
作者：记者甲
发布时间：2026-08-21 10:00:00

某型火箭今天完成回收试验。
一、试验过程
火箭按计划升空并返回着陆场。
二、后续计划
团队将继续开展重复使用测试。`;

describe('news semantic chunks', () => {
  it('parses JSON wrapped in model output', () => {
    const result = parseNewsAnalysis(`结果如下：\n\`\`\`json
{
  "summary": "火箭完成回收试验",
  "entities": [],
  "facts": []
}
\`\`\``);

    expect(result.summary).toBe('火箭完成回收试验');
  });

  it('keeps entity analysis backward compatible when aliases are omitted', () => {
    const result = parseNewsAnalysis(`{
      "summary": "人物新闻",
      "entities": [{ "name": "C罗", "type": "person" }],
      "facts": []
    }`);

    expect(result.entities[0]?.aliases).toEqual([]);
  });

  it('adds global indexes to article and evidence-related indexes to paragraph', async () => {
    const analyze: NewsAnalyzer = vi.fn().mockResolvedValue({
      summary: '火箭完成回收试验并计划继续测试。',
      entities: [{ name: '某型火箭', type: 'product', aliases: ['试验火箭', '某型火箭'] }],
      facts: [
        {
          subject: '火箭',
          predicate: '返回',
          object: '着陆场',
          time: '2026-08-21',
          location: '',
          evidence: '火箭按计划升空并返回着陆场。'
        }
      ]
    });

    const result = await createNewsDatasetChunks({
      text: newsText,
      chunkSize: 160,
      llmModel: 'test-model',
      teamId: 'test-team',
      imageIdList: ['image-1'],
      retryCount: 0,
      analyze
    });

    expect(result[0].indexes).toContain('火箭完成回收试验并计划继续测试。');
    expect(result[0].indexes).toContain('某型火箭 product');
    expect(result[0].indexes).toContain('某型火箭 试验火箭 product');
    expect(result.find((chunk) => chunk.q.includes('返回着陆场'))?.indexes).toContain(
      '火箭 返回 着陆场 2026-08-21'
    );
    const factChunk = result.find((chunk) => chunk.metadata?.chunkType === 'fact');
    expect(factChunk).toEqual(
      expect.objectContaining({
        q: '火箭 返回 着陆场 2026-08-21',
        a: '火箭按计划升空并返回着陆场。',
        indexes: [],
        metadata: expect.objectContaining({
          chunkType: 'fact',
          parentChunkIndex: expect.any(Number),
          fact: {
            subject: '火箭',
            predicate: '返回',
            object: '着陆场',
            time: '2026-08-21',
            location: ''
          }
        })
      })
    );
    expect(result.every((chunk) => chunk.imageIdList?.[0] === 'image-1')).toBe(true);
    expect(result.every((chunk) => chunk.metadata?.analysisMode === 'llm')).toBe(true);
  });

  it('deduplicates independent fact chunks', async () => {
    const fact = {
      subject: '火箭',
      predicate: '返回',
      object: '着陆场',
      time: '2026-08-21',
      location: '',
      evidence: '火箭按计划升空并返回着陆场。'
    };
    const analyze: NewsAnalyzer = vi.fn().mockResolvedValue({
      summary: '火箭完成回收试验。',
      entities: [],
      facts: [fact, fact]
    });

    const result = await createNewsDatasetChunks({
      text: newsText,
      chunkSize: 160,
      llmModel: 'test-model',
      teamId: 'test-team',
      retryCount: 0,
      analyze
    });

    expect(result.filter((chunk) => chunk.metadata?.chunkType === 'fact')).toHaveLength(1);
  });

  it('retries and falls back to structural chunks without losing text chunks', async () => {
    const analyze = vi.fn().mockRejectedValue(new Error('model unavailable'));

    const result = await createNewsDatasetChunks({
      text: newsText,
      chunkSize: 160,
      llmModel: 'test-model',
      teamId: 'test-team',
      retryCount: 1,
      analyze
    });

    expect(analyze).toHaveBeenCalledTimes(2);
    expect(result.length).toBeGreaterThan(1);
    expect(result.every((chunk) => chunk.indexes.length === 0)).toBe(true);
    expect(result.every((chunk) => chunk.metadata?.analysisMode === 'fallback')).toBe(true);
  });

  it('rejects invalid retry and timeout settings before invoking the model', async () => {
    const analyze: NewsAnalyzer = vi.fn();

    await expect(
      createNewsDatasetChunks({
        text: newsText,
        chunkSize: 160,
        llmModel: 'test-model',
        teamId: 'test-team',
        timeout: 0,
        analyze
      })
    ).rejects.toThrow('timeout must be positive');
    expect(analyze).not.toHaveBeenCalled();
  });
});
