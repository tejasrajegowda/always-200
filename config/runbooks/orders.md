orders serves POST /orders: it inserts an order, reads the customer's 5 latest orders, calls payments, then marks the order paid.
It uses Postgres for both writes and the read, so database slowness shows up as orders latency.
Any failure or non-200 answer from payments becomes a 502 here.
If orders alerts but payments does not, check orders' latest deploy and its database queries first.
Its health check is GET /healthz; POST /admin/restart exits the process and Compose restarts it.
