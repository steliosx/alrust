vcl 4.1;
backend default { .host = "127.0.0.1"; .port = "@PORT@"; }
sub vcl_recv {
    if (req.method != "GET" || req.http.Cookie || req.http.X-API-Key) { return(pass); }
    if (req.http.Authorization !~ "^Key bench-replay-[0-9a-f]{40}$") { return(pass); }
    if (req.url !~ "^/alerts/count$|^/alerts[?]sort-by=(resource|lastReceiveTime)&page=1&page-size=100&environment=Production(&severity=major)?(&_=[0-9]+-[0-9]+)?$") { return(pass); }
    return(hash);
}
sub vcl_hash {
    hash_data(req.http.host);
    hash_data(req.url);
    hash_data(req.http.Authorization);
    hash_data(req.http.Accept-Encoding);
    return(lookup);
}
sub vcl_backend_response {
    set beresp.grace = 0s;
    set beresp.keep = 0s;
    if (beresp.status != 200 || beresp.http.Set-Cookie || beresp.http.Vary == "*"
        || beresp.http.Cache-Control ~ "(?i)no-store|no-cache|private|must-revalidate|proxy-revalidate"
        || beresp.http.Pragma ~ "(?i)no-cache" || beresp.ttl <= 0s) {
        set beresp.uncacheable = true;
        return(deliver);
    }
    if (beresp.ttl > 1s) { set beresp.ttl = 1s; }
    return(deliver);
}
sub vcl_deliver {
    if (obj.hits > 0) { set resp.http.X-Bench-Cache = "hit"; }
    else { set resp.http.X-Bench-Cache = "origin"; }
}
