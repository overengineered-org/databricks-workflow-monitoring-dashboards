CASE WHEN COUNT(DISTINCT CASE WHEN r.result_state = 'SUCCEEDED' THEN r.$run_identity END) > 0 THEN 'Met' WHEN d.deadline_utc <= $current_instant THEN 'Missed' ELSE 'Pending' END
