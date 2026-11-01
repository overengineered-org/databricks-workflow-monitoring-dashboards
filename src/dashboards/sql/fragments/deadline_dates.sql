  SELECT w.*, deadline_date FROM workflow_local_dates w LATERAL VIEW EXPLODE(sequence(date_sub(local_today, $daily_window_start_days), date_add(local_today, $daily_window_end_days))) d AS deadline_date WHERE frequency = 'daily'
  UNION ALL
  SELECT w.*, date_add($weekly_anchor_date, offset * 7) AS deadline_date FROM workflow_local_dates w LATERAL VIEW EXPLODE(sequence($weekly_offset_start, $weekly_offset_end)) o AS offset WHERE frequency = 'weekly'
  UNION ALL
  SELECT w.*, date_add(first_deadline_date, (GREATEST(CAST(FLOOR(datediff(local_today, first_deadline_date) / 14.0) AS INT), 0) + offset) * 14) AS deadline_date FROM workflow_local_dates w LATERAL VIEW EXPLODE(sequence($fortnightly_offset_start, $fortnightly_offset_end)) o AS offset WHERE frequency = 'fortnightly'
  UNION ALL
  SELECT w.*, date_add(to_date(add_months(date_trunc('MONTH', local_today), offset)), LEAST(day_of_month, day(last_day(add_months(local_today, offset)))) - 1) AS deadline_date FROM workflow_local_dates w LATERAL VIEW EXPLODE(sequence($monthly_offset_start, $monthly_offset_end)) o AS offset WHERE frequency = 'monthly'
