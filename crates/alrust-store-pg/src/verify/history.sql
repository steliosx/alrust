SELECT count(*)
  FROM (SELECT a.id, h.n, h.e
          FROM alerts_alpha a CROSS JOIN LATERAL jsonb_array_elements(a.history) WITH ORDINALITY AS h(e, n)) o
  FULL JOIN (SELECT a.id, h.*
          FROM alerts a CROSS JOIN LATERAL unnest(a.history)
               WITH ORDINALITY AS h(hid, event, severity, status, value, text, type, update_time, usr, timeout, n)) r
    ON r.id = o.id AND r.n = o.n
 WHERE o.id IS NULL OR r.id IS NULL
    OR r.hid IS DISTINCT FROM (o.e->>'id')
    OR r.event IS DISTINCT FROM (o.e->>'event')
    OR r.severity IS DISTINCT FROM (o.e->>'severity')
    OR r.status IS DISTINCT FROM (o.e->>'status')
    OR r.value IS DISTINCT FROM (o.e->'value' #>> '{}')
    OR r.text IS DISTINCT FROM (o.e->>'text')
    OR r.type IS DISTINCT FROM (o.e->>'type')
    OR r.update_time IS DISTINCT FROM ((o.e->>'updateTime')::timestamptz AT TIME ZONE 'UTC')
    OR (r.update_time AT TIME ZONE 'UTC') IS DISTINCT FROM (o.e->>'updateTime')::timestamptz
    OR r.usr IS DISTINCT FROM (o.e->>'user')
    OR r.timeout::bigint IS DISTINCT FROM (o.e->>'timeout')::bigint
