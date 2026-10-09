payments serves POST /payments and inserts one payment row in Postgres; it calls no other service.
It is the deepest service in the graph, so its faults travel up to orders and storefront.
If payments alerts, check its latest deploy and the payments table insert time first.
An injected fault answers 503 with "injected fault"; GET /admin/fault shows the active one.
Its health check is GET /healthz; POST /admin/restart exits the process and Compose restarts it.
