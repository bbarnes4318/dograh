-- Day-one review of the call hygiene guards. READ-ONLY: every statement is a
-- SELECT inside a READ ONLY transaction.
--
-- Run with day_one_review.sh, which passes:
--   :review_day   the day to review, e.g. '2026-10-01'
--   :baseline_day the comparison day, default '2026-09-25'
--   :tz           the calling timezone, default 'America/New_York'
--   :ui_base      the UI base URL for run links
--
-- "Connected" = the call has a stored duration > 0. Calls from before the
-- call-duration fix (9/26) only have one if the backfill was run; the
-- baseline block prints how many 9/25 calls have a duration so you can see
-- whether the comparison is fair.

BEGIN READ ONLY;

\echo
\echo '== 1. Calls by disposition (review day) =='
SELECT coalesce(gathered_context->>'call_disposition', '(none)') AS call_disposition,
       count(*) AS calls
FROM workflow_runs
WHERE (created_at AT TIME ZONE :'tz')::date = :'review_day'::date
  AND mode <> 'textchat'
GROUP BY 1
ORDER BY 2 DESC;

\echo
\echo '== 2. Call hygiene tags (review day) =='
WITH tags(tag) AS (
    VALUES ('vm_keyword_guard'), ('vm_after_screener'), ('call_screener_no_pickup'),
           ('answering_bot'), ('no_speech_dead_air'), ('closing_line_watchdog'),
           ('llm_markup_leak'), ('transfer_blocked_machine')
)
SELECT t.tag,
       count(r.id) AS calls,
       coalesce(round(avg(nullif((r.usage_info->>'call_duration_seconds')::numeric, 0)), 1), 0)
           AS avg_seconds
FROM tags t
LEFT JOIN workflow_runs r
  ON (r.created_at AT TIME ZONE :'tz')::date = :'review_day'::date
 AND r.mode <> 'textchat'
 AND (r.gathered_context->'call_tags')::jsonb ? t.tag
GROUP BY t.tag
ORDER BY 2 DESC;

\echo
\echo '== 3. Transfers and connected time: review day vs baseline =='
WITH days AS (
    SELECT 'review'   AS which, :'review_day'::date   AS day
    UNION ALL
    SELECT 'baseline' AS which, :'baseline_day'::date AS day
),
runs AS (
    SELECT d.which, r.*,
           coalesce((r.usage_info->>'call_duration_seconds')::numeric, 0) AS secs
    FROM days d
    JOIN workflow_runs r
      ON (r.created_at AT TIME ZONE :'tz')::date = d.day
     AND r.mode <> 'textchat'
     AND r.campaign_id IS NOT NULL
)
SELECT which,
       count(*)                                                         AS dialed,
       count(*) FILTER (WHERE secs > 0)                                 AS connected,
       count(*) FILTER (WHERE gathered_context->>'transfer_state' IS NOT NULL)
                                                                        AS transfer_attempts,
       count(*) FILTER (WHERE gathered_context->>'transfer_state'
                              IN ('complete', 'completed', 'terminated'))
                                                                        AS transfers_connected,
       round(100.0 * count(*) FILTER (WHERE gathered_context->>'transfer_state'
                                            IN ('complete', 'completed', 'terminated'))
             / nullif(count(*) FILTER (WHERE secs > 0), 0), 2)          AS transfer_rate_pct_of_connected,
       round(sum(secs))                                                 AS connected_seconds,
       round(avg(secs) FILTER (WHERE secs > 0), 1)                      AS avg_connected_seconds,
       count(*) FILTER (WHERE (gathered_context->'call_tags')::jsonb ? 'voicemail_detected')
                                                                        AS voicemail_detected
FROM runs
GROUP BY which
ORDER BY which DESC;

\echo
\echo '== 4. 30 random vm_keyword_guard calls: listen for real people =='
\echo '   (open the link; the run page plays the recording)'
SELECT r.id AS run_id,
       to_char(r.created_at AT TIME ZONE :'tz', 'HH24:MI:SS') AS at,
       r.initial_context->>'called_number' AS called,
       coalesce(r.usage_info->>'call_duration_seconds', '?') AS secs,
       :'ui_base' || '/workflow/' || r.workflow_id || '/run/' || r.id AS link,
       r.recording_url AS recording_key,
       left(regexp_replace(coalesce(r.transcript_text, '(no transcript)'), '\s+', ' ', 'g'), 400)
           AS transcript
FROM workflow_runs r
WHERE (r.created_at AT TIME ZONE :'tz')::date = :'review_day'::date
  AND (r.gathered_context->'call_tags')::jsonb ? 'vm_keyword_guard'
ORDER BY random()
LIMIT 30;

ROLLBACK;
