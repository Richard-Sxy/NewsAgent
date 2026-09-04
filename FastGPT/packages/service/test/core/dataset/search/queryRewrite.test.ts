import { describe, expect, it, vi } from 'vitest';
import {
  buildAdaptiveSearchQueryPlan,
  buildRuleBasedQueryPlan,
  buildSearchQueryPlan,
  parseSearchQueryPlan,
  shouldUseLLMQueryRewrite,
  type QueryRewriter
} from '../../../../core/dataset/search/queryRewrite';
import { analyzeSearchQuery } from '../../../../core/dataset/search/queryAnalysis';

describe('news query rewrite', () => {
  it('parses and validates model JSON', () => {
    const result = parseSearchQueryPlan(`结果：\n\`\`\`json
      {
        "semanticQuery": "C罗最近一场比赛进了几个球？",
        "keywordQueries": ["C罗 最近 比赛 进球"],
        "entities": [{"name":"C罗","aliases":["Cristiano Ronaldo"],"type":"person"}],
        "timeExpressions": ["最近一场"],
        "numberExpressions": [],
        "intent": "number"
      }
    \`\`\``);

    expect(result.keywordQueries).toEqual(['C罗 最近 比赛 进球']);
    expect(result.entities[0]?.aliases).toContain('Cristiano Ronaldo');
    expect(result.intent).toBe('number');
  });

  it('preserves original query when rewrite succeeds', async () => {
    const rewrite: QueryRewriter = vi.fn().mockResolvedValue({
      semanticQuery: '完整语义问题',
      keywordQueries: ['人物 时间 事件'],
      entities: [],
      timeExpressions: [],
      numberExpressions: [],
      intent: 'fact'
    });

    const result = await buildSearchQueryPlan({
      query: '原始问题',
      teamId: 'team',
      llmModel: 'model',
      rewrite
    });

    expect(result.originalQuery).toBe('原始问题');
    expect(result.keywordQueries).toEqual(['人物 时间 事件']);
  });

  it('falls back to the original query when rewrite fails', async () => {
    const rewrite: QueryRewriter = vi.fn().mockRejectedValue(new Error('timeout'));

    const result = await buildSearchQueryPlan({
      query: '原始问题',
      teamId: 'team',
      llmModel: 'model',
      rewrite
    });

    expect(result).toEqual({
      originalQuery: '原始问题',
      semanticQuery: '原始问题',
      keywordQueries: ['原始问题'],
      entities: [],
      timeExpressions: [],
      numberExpressions: [],
      intent: 'other'
    });
  });

  it('routes time and number signals without extra queries or invoking the LLM', async () => {
    const query = '腾讯2026年第二季度营收是多少？';
    const analysis = analyzeSearchQuery(query);
    const rewrite: QueryRewriter = vi.fn();

    const result = await buildAdaptiveSearchQueryPlan({
      query,
      analysis,
      teamId: 'team',
      llmModel: 'model',
      rewrite
    });

    expect(shouldUseLLMQueryRewrite(analysis)).toBe(false);
    expect(rewrite).not.toHaveBeenCalled();
    expect(result.keywordQueries).toEqual([query]);
    expect(result.timeExpressions).toEqual(['2026年', '第二季度']);
  });

  it('uses the LLM only for a detected person and preserves rule queries', async () => {
    const query = 'C罗最近一场比赛进了几个球？';
    const analysis = analyzeSearchQuery(query);
    const rewrite: QueryRewriter = vi.fn().mockResolvedValue({
      semanticQuery: query,
      keywordQueries: ['Cristiano Ronaldo 比赛 进球'],
      entities: [
        {
          name: 'C罗',
          aliases: ['Cristiano Ronaldo'],
          type: 'person'
        }
      ],
      timeExpressions: ['最近一场'],
      numberExpressions: [],
      intent: 'person'
    });

    const result = await buildAdaptiveSearchQueryPlan({
      query,
      analysis,
      teamId: 'team-person',
      llmModel: 'model',
      rewrite
    });

    expect(rewrite).toHaveBeenCalledTimes(1);
    expect(result.keywordQueries).toContain(query);
    expect(result.keywordQueries).toContain('Cristiano Ronaldo 比赛 进球');
  });

  it('builds a deterministic rule plan for exact signals', () => {
    const query = 'C罗在2026年进了3个球吗？';
    const plan = buildRuleBasedQueryPlan(query, analyzeSearchQuery(query));

    expect(plan.originalQuery).toBe(query);
    expect(plan.keywordQueries[0]).toBe(query);
    expect(plan.keywordQueries).toContain('C罗 2026年 3个');
  });
});
