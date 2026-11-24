# Dashboard 1 · Shop health & business

Dataset: the span index (`otel-v1-apm-span-*`, time field `startTime`). Build each panel in OpenSearch UI: Explore →
PPL → paste → Run → Visualize → chart type → Save → add to dashboard. If the query bar already supplies the
`source`, paste from `| where` onward. Time picker: last 24 hours, rounded to the hour.

Header (Markdown panel): `# Astronomy Shop · Health & business` / `##### Availability, orders, failures and the AI assistant, last 24 hours`

## Tiles (Metric; Text display = Value)

**Availability (%)**
```
source = otel-v1-apm-span-* | where kind = 'SPAN_KIND_SERVER' | eval ok = if(`status.code` != 2, 1, 0) | stats sum(ok) as ok, count() as total | eval availability_pct = round(ok * 100.0 / total, 2) | fields availability_pct
```
**Orders placed**
```
source = otel-v1-apm-span-* | where serviceName = 'checkout' and name = 'oteldemo.CheckoutService/PlaceOrder' and `status.code` != 2 | stats count() as orders
```
**Order value ($M)** — unit $, suffix M
```
source = otel-v1-apm-span-* | where serviceName = 'checkout' and name = 'oteldemo.CheckoutService/PlaceOrder' and `status.code` != 2 | stats sum(`attributes.demo.order.amount`) as v | eval order_value_m = round(v / 1000000, 2) | fields order_value_m
```
**Failed requests**
```
source = otel-v1-apm-span-* | where `status.code` = 2 | stats count() as failures
```
**Assistant conversations**
```
source = otel-v1-apm-span-* | where name = 'astronomy_shop_agent_workflow.workflow' | stats count() as conversations
```

## Charts

**Requests per 15 min** (bar, green)
```
source = otel-v1-apm-span-* | where kind = 'SPAN_KIND_SERVER' | stats count() as requests by span(startTime, 15m)
```
**Errors per 15 min** (bar, red). Errors are ~0.5% of traffic; a separate panel keeps them visible.
```
source = otel-v1-apm-span-* | where kind = 'SPAN_KIND_SERVER' and `status.code` = 2 | stats count() as errors by span(startTime, 15m)
```
**Failures by service** (donut)
```
source = otel-v1-apm-span-* | where `status.code` = 2 | stats count() as failures by serviceName | sort - failures | head 10
```
**Orders per hour** (bar)
```
source = otel-v1-apm-span-* | where serviceName = 'checkout' and name = 'oteldemo.CheckoutService/PlaceOrder' and `status.code` != 2 | stats count() as orders by span(startTime, 1h)
```
**Order value per hour ($K)** (line)
```
source = otel-v1-apm-span-* | where serviceName = 'checkout' and name = 'oteldemo.CheckoutService/PlaceOrder' and `status.code` != 2 | stats sum(`attributes.demo.order.amount`) as v by span(startTime, 1h) | eval order_value_k = round(v / 1000, 0)
```
**Assistant conversations per hour** (line)
```
source = otel-v1-apm-span-* | where name = 'astronomy_shop_agent_workflow.workflow' | stats count() as conversations by span(startTime, 1h)
```
**Latency by service: typical and tail (ms)** (table). Median and p95 differ by three orders of magnitude for
checkout, so a table is more honest than a bar chart.
```
source = otel-v1-apm-span-* | where kind = 'SPAN_KIND_SERVER' | stats percentile_approx(durationInNanos, 50) as p50, percentile_approx(durationInNanos, 95) as p95 by serviceName | eval `Typical (p50) ms` = round(p50 / 1000000, 0), `Tail (p95) ms` = round(p95 / 1000000, 0) | rename serviceName as Service | fields Service, `Typical (p50) ms`, `Tail (p95) ms` | sort - `Tail (p95) ms`
```

Span status codes: 0 unset, 1 OK, 2 error (OpenTelemetry). Most services never set OK explicitly, so "not 2" is the
success condition.
