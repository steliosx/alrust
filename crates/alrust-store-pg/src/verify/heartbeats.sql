SELECT count(*) FROM heartbeats_alpha o FULL JOIN heartbeats r ON r.id = o.id
 WHERE o.id IS NULL OR r.id IS NULL
    OR r.origin IS DISTINCT FROM o.origin
    OR r.tags IS DISTINCT FROM o.tags
    OR r.type IS DISTINCT FROM o.type
    OR r.create_time IS DISTINCT FROM (o.create_time AT TIME ZONE 'UTC')
    OR (r.create_time AT TIME ZONE 'UTC') IS DISTINCT FROM o.create_time
    OR r.timeout::bigint IS DISTINCT FROM o.timeout
    OR r.receive_time IS DISTINCT FROM (o.receive_time AT TIME ZONE 'UTC')
    OR (r.receive_time AT TIME ZONE 'UTC') IS DISTINCT FROM o.receive_time
    OR r.customer IS DISTINCT FROM o.customer
    OR r.attributes IS DISTINCT FROM o.attributes
