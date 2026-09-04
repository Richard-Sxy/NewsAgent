import { sliceJsonStr } from '@fastgpt/global/common/string/tools';
import type { OpenaiAccountType } from '@fastgpt/global/support/user/team/type';
import json5 from 'json5';
import { z } from 'zod';
import { getLogger, LogCategories } from '../../../common/logger';
import { createLLMResponse } from '../../ai/llm/request';
import { getLLMModel } from '../../ai/model';
import type { QueryAnalysis } from './queryAnalysis';

const logger = getLogger(LogCategories.MODULE.DATASET.DATA);

const SearchQueryPlanSchema = z.object({
  semanticQuery: z.string().trim().min(1),
  keywordQueries: z.array(z.string().trim().min(1).max(80)).min(1).max(3),
  entities: z
    .array(
      z.object({
        name: z.string().trim().min(1),
        aliases: z.array(z.string().trim().min(1)).max(5).default([]),
        type: z.enum(['person', 'organization', 'product', 'location', 'event', 'other'])
      })
    )
    .max(10)
    .default([]),
  timeExpressions: z.array(z.string().trim().min(1)).max(5).default([]),
  numberExpressions: z.array(z.string().trim().min(1)).max(5).default([]),
  intent: z.enum(['fact', 'person', 'time', 'number', 'comparison', 'causal', 'summary', 'other'])
});

export type SearchQueryPlan = z.infer<typeof SearchQueryPlanSchema> & { originalQuery: string };

export type QueryRewriter = (props: {
  query: string;
  teamId: string;
  llmModel: string;
  timeout: number;
  userKey?: OpenaiAccountType;
}) => Promise<Omit<SearchQueryPlan, 'originalQuery'>>;

const rewriteCache = new Map<string, { expiresAt: number; plan: SearchQueryPlan }>();
const REWRITE_CACHE_TTL = 10 * 60 * 1000;
const REWRITE_CACHE_MAX_SIZE = 500;

/** 查询文本构建模式 */
const QUERY_WORDS_PATTERN =
  /根据(?:新闻|报道|原文)|请问|是多少|有多少|为什么|为何|如何|怎样|哪些|什么|分别|情况|结果|具体|是否|有何|吗|呢|？|\?/g;

const uniqueStrings = (items: string[]) =>
  [...new Set(items.map((item) => item.replace(/\s+/g, ' ').trim()))].filter(Boolean);

const inferIntent = (analysis: QueryAnalysis): SearchQueryPlan['intent'] => {
  if (analysis.hasPerson) return 'person';
  if (analysis.hasTime) return 'time';
  if (analysis.hasNumber) return 'number';
  if (analysis.hasFactIntent) return 'fact';
  if (analysis.hasSemanticIntent) return 'causal';
  return 'other';
};

/**
 * 规则层只分析精确信号。时间和数值用于动态调权，但不单独扩展全文 query；
 * 实测额外短 query 会在 RRF 中稀释原问题。只有人名保留一条精确信号查询，供别名扩展融合。
 */
export const buildRuleBasedQueryPlan = (
  query: string,
  analysis: QueryAnalysis
): SearchQueryPlan => {
  const conciseQuery = query
    .replace(QUERY_WORDS_PATTERN, ' ')
    .replace(/[，。；：、！!（）()【】\[\]]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  const exactSignals = uniqueStrings([
    ...analysis.matchedNames,
    ...analysis.matchedTimes,
    ...analysis.matchedNumbers
  ]).join(' ');

  const keywordQueries = analysis.hasPerson
    ? uniqueStrings([query, conciseQuery, exactSignals]).slice(0, 3)
    : [query];

  return {
    originalQuery: query,
    semanticQuery: query,
    keywordQueries,
    entities: analysis.matchedNames.map((name) => ({ name, aliases: [], type: 'person' })),
    timeExpressions: analysis.matchedTimes,
    numberExpressions: analysis.matchedNumbers,
    intent: inferIntent(analysis)
  };
};

/** 只有人名可能需要模型补充规范名和别名；普通时间、数值问题不调用模型。 */
export const shouldUseLLMQueryRewrite = (analysis: QueryAnalysis) => analysis.hasPerson;

const QUERY_REWRITE_SYSTEM_PROMPT = `你是新闻知识库检索查询规划器。
你的任务不是回答问题，而是把用户问题转换成适合新闻知识库检索的结构化查询。

要求：
1. semanticQuery 必须保留完整语义，不得改变问题目标。
2. keywordQueries 保留可区分新闻的人物、机构、产品、赛事、地点、时间、数值和动作。
3. 只补充能够明确确认的常用别名、简称或英文名，禁止猜测。
4. 保留否定、比较、先后顺序、主体、相对时间和时间范围。
5. keywordQueries 返回 1 至 3 条，每条最多 80 个字符。
6. 不允许回答用户问题，只返回 JSON，不要返回 Markdown。

输出格式：
{
  "semanticQuery": "保留完整语义的问题",
  "keywordQueries": ["关键词查询"],
  "entities": [{
    "name": "实体规范名称",
    "aliases": ["明确可靠的别名"],
    "type": "person|organization|product|location|event|other"
  }],
  "timeExpressions": ["时间表达"],
  "numberExpressions": ["数值表达"],
  "intent": "fact|person|time|number|comparison|causal|summary|other"
}`;

const buildQueryRewritePrompt = (query: string) => `请为下面的问题生成新闻检索计划：

<user_query>
${query}
</user_query>`;

/** 解析查询计划 */
export const parseSearchQueryPlan = (answer: string) => {
  const jsonText = sliceJsonStr(answer);
  if (!jsonText) throw new Error('Query rewrite response has no JSON');
  return SearchQueryPlanSchema.parse(json5.parse(jsonText));
};

const requestQueryRewrite: QueryRewriter = async ({
  query,
  teamId,
  llmModel,
  timeout,
  userKey
}) => {
  const model = getLLMModel(llmModel);
  const { answerText } = await createLLMResponse({
    userKey,
    teamId,
    timeout,
    // createLLMResponse 以该值作为请求循环上限；1 表示至少执行一次请求。
    maxContinuations: 1,
    saveLLMResponseRecord: false,
    body: {
      model: model.model,
      stream: false,
      temperature: 0,
      messages: [
        { role: 'system', content: QUERY_REWRITE_SYSTEM_PROMPT },
        { role: 'user', content: buildQueryRewritePrompt(query) }
      ]
    }
  });

  return parseSearchQueryPlan(answerText);
};

/** 异步构建查询搜索计划 */
export const buildSearchQueryPlan = async ({
  query,
  teamId,
  llmModel,
  timeout = 5000,
  userKey,
  rewrite = requestQueryRewrite
}: {
  query: string;
  teamId: string;
  llmModel: string;
  timeout?: number;
  userKey?: OpenaiAccountType;
  rewrite?: QueryRewriter;
}): Promise<SearchQueryPlan> => {
  try {
    const plan = await rewrite({ query, teamId, llmModel, timeout, userKey });
    return { originalQuery: query, ...plan };
  } catch (error) {
    logger.warn('Query rewrite failed, using original query', { error });
    return {
      originalQuery: query,
      semanticQuery: query,
      keywordQueries: [query],
      entities: [],
      timeExpressions: [],
      numberExpressions: [],
      intent: 'other'
    };
  }
};

export const buildAdaptiveSearchQueryPlan = async ({
  query,
  analysis,
  teamId,
  llmModel,
  timeout = 5000,
  userKey,
  rewrite
}: {
  query: string;
  analysis: QueryAnalysis;
  teamId: string;
  llmModel: string;
  timeout?: number;
  userKey?: OpenaiAccountType;
  rewrite?: QueryRewriter;
}): Promise<SearchQueryPlan> => {
  const rulePlan = buildRuleBasedQueryPlan(query, analysis);
  if (!shouldUseLLMQueryRewrite(analysis)) return rulePlan;

  const cacheKey = `${teamId}\u0000${llmModel}\u0000${query}`;
  const cached = rewriteCache.get(cacheKey);
  if (cached && cached.expiresAt > Date.now()) return cached.plan;
  if (cached) rewriteCache.delete(cacheKey);

  const llmPlan = await buildSearchQueryPlan({
    query,
    teamId,
    llmModel,
    timeout,
    userKey,
    rewrite
  });
  const plan: SearchQueryPlan = {
    ...llmPlan,
    // 保留规则结果和原问题，避免模型删除时间、数字或其他重要限定条件。
    keywordQueries: uniqueStrings([...rulePlan.keywordQueries, ...llmPlan.keywordQueries]).slice(
      0,
      4
    ),
    timeExpressions: uniqueStrings([...rulePlan.timeExpressions, ...llmPlan.timeExpressions]),
    numberExpressions: uniqueStrings([...rulePlan.numberExpressions, ...llmPlan.numberExpressions])
  };

  // 失败降级结果没有新增信息，不缓存，下一次仍允许模型恢复后重试。
  const hasRewriteValue =
    plan.semanticQuery !== query ||
    plan.keywordQueries.some((item) => !rulePlan.keywordQueries.includes(item));
  if (hasRewriteValue) {
    if (rewriteCache.size >= REWRITE_CACHE_MAX_SIZE) {
      rewriteCache.delete(rewriteCache.keys().next().value!);
    }
    rewriteCache.set(cacheKey, { expiresAt: Date.now() + REWRITE_CACHE_TTL, plan });
  }

  return plan;
};
