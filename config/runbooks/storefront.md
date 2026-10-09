storefront serves POST /buy and forwards every purchase to orders; it has no store of its own.
Any failure or non-200 answer from orders becomes a 502 here, so storefront alerts on most faults.
If only storefront alerts, check its own latest deploy and its process restarts first.
Its health check is GET /healthz; POST /admin/restart exits the process and Compose restarts it.
Its latency includes the whole orders and payments chain, so a slow callee looks slow here too.
