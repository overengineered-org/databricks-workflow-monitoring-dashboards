to_utc_timestamp(to_timestamp(concat(CAST(deadline_date AS STRING), ' ', completion_time), 'yyyy-MM-dd HH:mm'), timezone)
