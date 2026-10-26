-- DBeaver: NewsAgent Unified News + Audience
-- 127.0.0.1:25432 / newsagent_unified_news_20261023
-- 每行是“一篇新闻 + 一个一分钟窗口”的模拟读者群体，不是某个真实用户。
-- 兴趣百分比以全部详情读者为分母；标签可重叠，不能相加。
-- Ctrl+Enter 查询；Alt+X 可执行脚本。重新查询才能看到数据库的新值。
-- 显示窗口起点位于“最新已存窗口起点减1分钟”及之后的记录；缺口时不保证有两个窗口。
-- 同步中断时仍能查看历史，务必核对窗口时间、最新事件时间、同步检查和定稿状态。
SELECT "窗口_北京时间","新闻标题","新闻内容标签","详情读者人数",
       "近24小时兴趣_TOP3","近30天兴趣_TOP3","窗口状态","同步检查",
       "曝光次数","点击次数","短期已画像人数","短期未知人数","短期冷启动人数",
       "长期已画像人数","长期未知人数","长期冷启动人数",
       "最新事件_北京时间","入库时间_北京时间","检查时间_北京时间",
       "数据来源","新闻链接","新闻正文","完整人群画像_JSON",news_id,window_key
FROM news_realtime.news_reader_overview
WHERE tenant_id='11111111-1111-4111-8111-111111111111'
  AND window_start >= (
    SELECT max(window_start)-interval '1 minute'
    FROM news_realtime.news_live_current
    WHERE tenant_id='11111111-1111-4111-8111-111111111111'
  )
ORDER BY window_start DESC,news_id;
