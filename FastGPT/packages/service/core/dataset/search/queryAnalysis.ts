export type QueryAnalysis = {
  hasPerson: boolean;
  hasNumber: boolean;
  hasTime: boolean;
  hasFactIntent: boolean;
  hasSemanticIntent: boolean;
  matchedNumbers: string[];
  matchedTimes: string[];
  matchedNames: string[];
};

const NUMBER_PATTERN = /\d+(?:\.\d+)?(?:%|％|亿美元|亿元|万元|元|万辆|万台|个|人|球|分)?/g;
const TIME_PATTERN =
  /\d{4}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?|\d{4}年|\d{1,2}月\d{1,2}日|第[一二三四1-4]季度|[Qq][1-4]|上半年|下半年|昨天|昨日|今日|今天|近期|最近|最新|本周|上周|本月|上月/g;
const FACT_INTENT_PATTERN =
  /多少|几(?:个|人|次|球|分|元|万|亿)?|何时|什么时候|谁|哪位|哪个|哪支|比分|营收|利润|同比|环比|价格|日期|进(?:了)?(?:多少|\d+(?:个)?)?球|金额|数量/;
const SEMANTIC_INTENT_PATTERN = /为什么|原因|影响|如何评价|怎么看|总结|概括|分析|意义/;
const EXPLICIT_PERSON_PATTERN =
  /C罗|梅西|[\u4e00-\u9fa5]{1,4}·[\u4e00-\u9fa5]{1,10}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+/g;
const CHINESE_PERSON_PATTERN =
  /(?:欧阳|司马|上官|诸葛|东方|皇甫|尉迟|公孙|慕容|司徒|令狐|张|王|李|赵|陈|刘|杨|黄|周|吴|徐|孙|胡|朱|高|林|何|郭|马|罗|梁|宋|郑|谢|韩|唐|冯|于|董|萧|程|曹|袁|邓|许|傅|沈|曾|彭|吕|苏|卢|蒋|蔡|贾|丁|魏|薛|叶|阎|余|潘|杜|戴|夏|钟|汪|田|任|姜|范|方|石|姚|谭|廖|邹|熊|金|陆|郝|孔|白|崔|康|毛|邱|秦|江|史|顾|侯|邵|孟|龙|万|段|雷|钱|汤|尹|黎|易|常|武|乔|贺|赖|龚|文)[\u4e00-\u9fa5]{1,2}(?=在|于|认为|表示|指出|宣布|获得|赢得|战胜|击败|对阵|开发|加盟|出席|回应|当时|担任|退役|夺得|的|将|已|曾|是|能|还)/g;

const unique = (items: string[]) => [...new Set(items)];

/**
 * 通过确定性规则识别查询中的精确召回信号。
 * 时间片段会先从数值识别文本中移除，避免年份同时触发时间和普通数值两次降权。
 */
export const analyzeSearchQuery = (query: string): QueryAnalysis => {
  const matchedTimes = query.match(TIME_PATTERN) ?? [];
  const queryWithoutTimes = query.replace(TIME_PATTERN, ' ');
  const matchedNumbers = queryWithoutTimes.match(NUMBER_PATTERN) ?? [];
  const matchedNames = [
    ...(query.match(EXPLICIT_PERSON_PATTERN) ?? []),
    ...(query.match(CHINESE_PERSON_PATTERN) ?? [])
  ];

  return {
    hasPerson: matchedNames.length > 0,
    hasNumber: matchedNumbers.length > 0,
    hasTime: matchedTimes.length > 0,
    hasFactIntent: FACT_INTENT_PATTERN.test(query),
    hasSemanticIntent: SEMANTIC_INTENT_PATTERN.test(query),
    matchedNumbers: unique(matchedNumbers),
    matchedTimes: unique(matchedTimes),
    matchedNames: unique(matchedNames)
  };
};

/**
 * 基于查询特征调整混合召回中的向量权重。
 * 精确实体、数值、时间和事实问题提高全文召回占比，解释性问题提高向量召回占比。
 */
export const calculateDynamicEmbeddingWeight = ({
  analysis,
  baseWeight = 0.5
}: {
  analysis: QueryAnalysis;
  baseWeight?: number;
}) => {
  let weight = baseWeight;

  if (analysis.hasPerson) weight -= 0.15;
  if (analysis.hasNumber) weight -= 0.1;
  if (analysis.hasTime) weight -= 0.1;
  if (analysis.hasFactIntent) weight -= 0.1;
  if (analysis.hasSemanticIntent) weight += 0.2;

  return Math.min(0.8, Math.max(0.2, weight));
};
