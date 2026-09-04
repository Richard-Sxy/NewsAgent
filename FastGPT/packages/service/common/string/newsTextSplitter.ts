import { splitText2Chunks } from './textSplitter';

export type NewsTextSplitProps = {
  text: string;
  chunkSize: number;
  overlapRatio?: number;
};

export type NewsTextSplitResult = {
  chunks: string[];
  chars: number;
};

export type ParsedNews = {
  title: string;
  source: string;
  author: string;
  publishTime: string;
  lead: string;
  paragraphs: string[];
};

const readMetadataValue = (line: string | undefined, label: string) =>
  line?.replace(new RegExp(`^${label}[：:]\\s*`), '').trim() || '';

/** 只识别具有明确格式的小标题，避免把短新闻句子误判为标题。 */
const isHeading = (text: string): boolean => {
  if (/^#{1,6}\s+\S/.test(text)) return true;
  if (/^[一二三四五六七八九十]{1,3}[、.．]\s*\S/.test(text)) return true;
  if (/^[（(][一二三四五六七八九十]{1,3}[）)]\s*\S/.test(text)) return true;
  if (/^\d{1,2}[、.．]\s*\S/.test(text)) return true;
  return text.length <= 30 && /^[^，。！？!?]{2,}[：:]$/.test(text);
};

/** 过滤独立成段的图片来源和编辑署名，不匹配“作者认为”等正常新闻句子。 */
const isNoiseParagraph = (text: string): boolean => {
  const normalized = text.replace(/\s+/g, ' ').trim();
  if (/^(?:举报|责任编辑[：:]?)$/.test(normalized)) return true;

  return (
    /^(?:作者|编辑|责编|责任编辑|记者|撰文|文|校对|审核|图源|图片来源|图片|摄影|摄图|供图)[：:]\s*\S.{0,100}$/.test(
      normalized
    ) || /^(?:图源|图片来源|图片|摄影|摄图|供图)\s+\S.{0,100}$/.test(normalized)
  );
};

/** 通过格式化HTML对象来解析正文文本内容，通过正则去匹配内容。 */
export const parseNewsText = (text: string): ParsedNews => {
  const cleanedText = text.replace(
    /<!--\s*NO_READ_BEGIN\s*-->[\s\S]*?<!--\s*NO_READ_END\s*-->/gi,
    ''
  );
  const lines = cleanedText
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .map((line) => line.trim());

  const titleIndex = lines.findIndex((line) => /^#\s+\S/.test(line));
  const sourceIndex = lines.findIndex((line) => /^来源地址[：:]/.test(line));
  const authorIndex = lines.findIndex((line) => /^作者[：:]/.test(line));
  const publishTimeIndex = lines.findIndex((line) => /^发布时间[：:]/.test(line));
  const metadataEndIndex = Math.max(titleIndex, sourceIndex, authorIndex, publishTimeIndex);

  const paragraphs = lines
    .slice(metadataEndIndex + 1)
    .filter(Boolean)
    .filter((line) => !/^(来源地址|作者|发布时间)[：:]/.test(line))
    .filter((line) => !isNoiseParagraph(line));

  return {
    title: titleIndex >= 0 ? lines[titleIndex].replace(/^#\s+/, '').trim() : '',
    source: readMetadataValue(sourceIndex >= 0 ? lines[sourceIndex] : undefined, '来源地址'),
    author: readMetadataValue(authorIndex >= 0 ? lines[authorIndex] : undefined, '作者'),
    publishTime: readMetadataValue(
      publishTimeIndex >= 0 ? lines[publishTimeIndex] : undefined,
      '发布时间'
    ),
    lead: paragraphs.find((paragraph) => !isHeading(paragraph)) || '',
    paragraphs
  };
};
// 标题归一化
const normalizeHeading = (text: string) =>
  text
    .replace(/^#{1,6}\s+/, '')
    .replace(/^[一二三四五六七八九十]{1,3}[、.．]\s*/, '')
    .replace(/^[（(][一二三四五六七八九十]{1,3}[）)]\s*/, '')
    .replace(/^\d{1,2}[、.．]\s*/, '')
    .replace(/[：:]$/, '')
    .trim();

const groupParagraphs = (paragraphs: string[]) => {
  const groups: Array<{ heading: string; paragraphs: string[] }> = [];
  let current = { heading: '', paragraphs: [] as string[] };

  for (const paragraph of paragraphs) {
    if (isHeading(paragraph)) {
      if (current.paragraphs.length > 0) groups.push(current);
      current = { heading: normalizeHeading(paragraph), paragraphs: [] };
    } else {
      current.paragraphs.push(paragraph);
    }
  }

  if (current.paragraphs.length > 0) groups.push(current);
  return groups;
};

const buildContextPrefix = (news: ParsedNews, heading: string, maxLength: number) =>
  [
    `文章标题：${news.title}`,
    heading ? `段落主题：${heading}` : '',
    news.publishTime ? `发布时间：${news.publishTime}` : '',
    news.lead ? `新闻导语：${news.lead}` : ''
  ]
    .filter(Boolean)
    .join('\n')
    .slice(0, maxLength)
    .trim();

// 文章索引
const buildArticleChunk = (news: ParsedNews, chunkSize: number) =>
  [
    `文章标题：${news.title}`,
    news.publishTime ? `发布时间：${news.publishTime}` : '',
    news.author ? `作者：${news.author}` : '',
    news.source ? `来源地址：${news.source}` : '',
    news.lead ? `新闻导语：${news.lead}` : ''
  ]
    .filter(Boolean)
    .join('\n')
    .slice(0, chunkSize)
    .trim();

/** 语义边界无法容纳单个超长自然段时，执行不会丢字的最终字符级保护切分。 */
const splitOversizedParagraph = (
  paragraph: string,
  maxLength: number,
  overlapRatio: number
): string[] => {
  const chars = Array.from(paragraph);
  const overlapLength = Math.floor(maxLength * overlapRatio);
  const step = Math.max(1, maxLength - overlapLength);
  const chunks: string[] = [];

  for (let start = 0; start < chars.length; start += step) {
    chunks.push(chars.slice(start, start + maxLength).join(''));
    if (start + maxLength >= chars.length) break;
  }

  return chunks;
};

const splitParagraphGroup = ({
  news,
  heading,
  paragraphs,
  chunkSize,
  overlapRatio
}: {
  news: ParsedNews;
  heading: string;
  paragraphs: string[];
  chunkSize: number;
  overlapRatio: number;
}): string[] => {
  const prefixBudget = Math.max(1, Math.floor(chunkSize * 0.4));
  const prefix = buildContextPrefix(news, heading, prefixBudget);
  const separator = prefix ? '\n\n' : '';
  const bodyBudget = Math.max(1, chunkSize - prefix.length - separator.length);
  const result: string[] = [];
  let currentParagraphs: string[] = [];
  let currentLength = 0;

  const flush = () => {
    if (currentParagraphs.length === 0) return;
    result.push(`${prefix}${separator}${currentParagraphs.join('\n\n')}`.trim());
    currentParagraphs = [];
    currentLength = 0;
  };

  for (const paragraph of paragraphs) {
    if (paragraph.length > bodyBudget) {
      flush();
      result.push(
        ...splitOversizedParagraph(paragraph, bodyBudget, overlapRatio).map((chunk) =>
          `${prefix}${separator}${chunk}`.trim()
        )
      );
      continue;
    }

    const separatorLength = currentParagraphs.length > 0 ? 2 : 0;
    if (currentLength + separatorLength + paragraph.length > bodyBudget) flush();
    currentParagraphs.push(paragraph);
    currentLength += separatorLength + paragraph.length;
  }

  flush();
  return result;
};

/**
 * 同步新闻切分入口：生成文章级表示和带标题、导语上下文的段落级分片。
 * 语义摘要、实体和事实抽取由后续异步服务完成。
 */
export function newsText2Chunks({
  text,
  chunkSize,
  overlapRatio = 0.2
}: NewsTextSplitProps): NewsTextSplitResult {
  if (chunkSize <= 0) throw new Error('chunkSize must be greater than 0');

  const normalizedText = text.trim();
  if (!normalizedText) return { chunks: [], chars: 0 };

  const news = parseNewsText(normalizedText);
  if (!news.title || news.paragraphs.length === 0) {
    return splitText2Chunks({ text: normalizedText, chunkSize, overlapRatio });
  }

  const chunks = [buildArticleChunk(news, chunkSize)];
  // 导语不一定是第一个原始段落，小标题或噪声可能位于它之前，因此按实际位置删除。
  const leadIndex = news.paragraphs.indexOf(news.lead);
  const bodyParagraphs = news.paragraphs.filter((_, index) => index !== leadIndex);
  for (const group of groupParagraphs(bodyParagraphs)) {
    chunks.push(
      ...splitParagraphGroup({
        news,
        heading: group.heading,
        paragraphs: group.paragraphs,
        chunkSize,
        overlapRatio
      })
    );
  }

  return {
    chunks: [...new Set(chunks.map((chunk) => chunk.trim()).filter(Boolean))],
    chars: normalizedText.length
  };
}
