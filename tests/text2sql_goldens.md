问题1：最新发布的 10 条新闻是什么
SQL:
SELECT title, publish_time, source
FROM news
ORDER BY publish_time DESC
LIMIT 10;

问题2：每个来源各有多少条新闻
SQL:
SELECT source, COUNT(*) AS news_count
FROM news
GROUP BY source
ORDER BY news_count DESC;

问题3：标题里包含“特朗普”的新闻有哪些
SQL:
SELECT title, publish_time, source
FROM news
WHERE title LIKE '%特朗普%'
ORDER BY publish_time DESC
LIMIT 10;