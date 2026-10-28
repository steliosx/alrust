SELECT count(*) FROM alrust_keys_before o FULL JOIN keys r ON r.id = o.id
 WHERE o.id IS NULL OR r.id IS NULL
    OR r.key IS DISTINCT FROM o.key
    OR r."user" IS DISTINCT FROM o."user"
    OR r.scopes IS DISTINCT FROM o.scopes
    OR r.text IS DISTINCT FROM o.text
    OR r.expire_time IS DISTINCT FROM (o.expire_time AT TIME ZONE 'UTC')
    OR (r.expire_time AT TIME ZONE 'UTC') IS DISTINCT FROM o.expire_time
    OR r.count IS DISTINCT FROM o.count
    OR r.last_used_time IS DISTINCT FROM (o.last_used_time AT TIME ZONE 'UTC')
    OR (r.last_used_time AT TIME ZONE 'UTC') IS DISTINCT FROM o.last_used_time
    OR r.customer IS DISTINCT FROM o.customer
