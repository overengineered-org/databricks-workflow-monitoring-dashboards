CASE WHEN COUNT(DISTINCT CASE WHEN r.result_state = 'SUCCEEDED' THEN r.run_id END) > 0 THEN 'Met' WHEN d.deadline_utc <= current_timestamp() THEN 'Missed' ELSE 'Pending' END
