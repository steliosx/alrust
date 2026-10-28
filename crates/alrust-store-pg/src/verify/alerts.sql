SELECT count(*) FROM alerts_alpha o FULL JOIN alerts r ON r.id = o.id
 WHERE o.id IS NULL OR r.id IS NULL
    OR r.resource IS DISTINCT FROM o.resource
    OR r.event IS DISTINCT FROM o.event
    OR r.environment IS DISTINCT FROM o.environment
    OR r.severity IS DISTINCT FROM o.severity
    OR r.correlate IS DISTINCT FROM o.correlate
    OR r.status IS DISTINCT FROM o.status
    OR r.service IS DISTINCT FROM o.service
    OR r."group" IS DISTINCT FROM o."group"
    OR r.value IS DISTINCT FROM (o.value #>> '{}')
    OR r.text IS DISTINCT FROM o.text
    OR r.tags IS DISTINCT FROM o.tags
    OR r.attributes IS DISTINCT FROM o.attributes
    OR r.origin IS DISTINCT FROM o.origin
    OR r.type IS DISTINCT FROM o.type
    OR r.create_time IS DISTINCT FROM (o.create_time AT TIME ZONE 'UTC')
    OR (r.create_time AT TIME ZONE 'UTC') IS DISTINCT FROM o.create_time
    OR r.timeout::bigint IS DISTINCT FROM o.timeout
    OR r.raw_data IS DISTINCT FROM o.raw_data
    OR r.customer IS DISTINCT FROM o.customer
    OR r.duplicate_count::bigint IS DISTINCT FROM o.duplicate_count
    OR r.repeat IS DISTINCT FROM o.repeat
    OR r.previous_severity IS DISTINCT FROM o.previous_severity
    OR r.trend_indication IS DISTINCT FROM o.trend_indication
    OR r.receive_time IS DISTINCT FROM (o.receive_time AT TIME ZONE 'UTC')
    OR (r.receive_time AT TIME ZONE 'UTC') IS DISTINCT FROM o.receive_time
    OR r.last_receive_id IS DISTINCT FROM o.last_receive_id
    OR r.last_receive_time IS DISTINCT FROM (o.last_receive_time AT TIME ZONE 'UTC')
    OR (r.last_receive_time AT TIME ZONE 'UTC') IS DISTINCT FROM o.last_receive_time
    OR r.update_time IS DISTINCT FROM (o.update_time AT TIME ZONE 'UTC')
    OR (r.update_time AT TIME ZONE 'UTC') IS DISTINCT FROM o.update_time
    OR cardinality(r.history) IS DISTINCT FROM jsonb_array_length(o.history)
