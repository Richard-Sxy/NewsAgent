import { describe, expect, it } from 'vitest';
import {
  analyzeSearchQuery,
  calculateDynamicEmbeddingWeight
} from '../../../../core/dataset/search/queryAnalysis';

describe('news query analysis', () => {
  it('recognizes person, relative time and fact intent', () => {
    const result = analyzeSearchQuery('C罗最近一场比赛进了几个球？');

    expect(result).toEqual(
      expect.objectContaining({
        hasPerson: true,
        hasTime: true,
        hasFactIntent: true,
        matchedNames: ['C罗'],
        matchedTimes: ['最近']
      })
    );
  });

  it('recognizes a common Chinese person name from its context', () => {
    const result = analyzeSearchQuery('张立非开发系统花费多少？');

    expect(result.hasPerson).toBe(true);
    expect(result.matchedNames).toContain('张立非');
    expect(result.hasFactIntent).toBe(true);
  });

  it('recognizes Chinese names in common sports-news contexts', () => {
    expect(analyzeSearchQuery('韩悦击败了哪位选手？').matchedNames).toContain('韩悦');
    expect(analyzeSearchQuery('杜丽当时担任什么角色？').matchedNames).toContain('杜丽');
  });

  it('does not count a year as an additional ordinary number', () => {
    const result = analyzeSearchQuery('腾讯2026年第二季度营收是多少？');

    expect(result.hasTime).toBe(true);
    expect(result.matchedTimes).toEqual(['2026年', '第二季度']);
    expect(result.hasNumber).toBe(false);
  });

  it('recognizes semantic intent and raises embedding weight', () => {
    const analysis = analyzeSearchQuery('为什么新能源汽车销量增长？');

    expect(analysis.hasSemanticIntent).toBe(true);
    expect(calculateDynamicEmbeddingWeight({ analysis })).toBe(0.7);
  });

  it('lowers embedding weight for an exact fact query and clamps the result', () => {
    const analysis = analyzeSearchQuery('C罗在2026年比赛中进了3个球吗？');

    expect(analysis).toEqual(
      expect.objectContaining({
        hasPerson: true,
        hasNumber: true,
        hasTime: true,
        hasFactIntent: true
      })
    );
    expect(calculateDynamicEmbeddingWeight({ analysis })).toBe(0.2);
  });
});
