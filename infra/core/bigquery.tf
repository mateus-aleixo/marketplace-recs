# The batch path's warehouse: the month of events, and the tables the nightly job builds
# from them, one partition per night. Everything here is derived from the public data, so
# it goes with the stack.
resource "google_bigquery_dataset" "recs" {
  dataset_id                 = "recs"
  location                   = var.region
  description                = "Marketplace events and the nightly recommendation tables built from them"
  delete_contents_on_destroy = true
  depends_on                 = [google_project_service.api["bigquery.googleapis.com"]]
}

resource "google_bigquery_table" "events" {
  dataset_id          = google_bigquery_dataset.recs.dataset_id
  table_id            = "events"
  description         = "Every view, cart and purchase of October 2019, from REES46"
  deletion_protection = false
  clustering          = ["product_id"]

  time_partitioning {
    type  = "DAY"
    field = "event_time"
  }

  schema = jsonencode([
    { name = "event_time", type = "TIMESTAMP", mode = "NULLABLE", description = "When it happened, UTC" },
    { name = "event_type", type = "INTEGER", mode = "NULLABLE", description = "0 view, 1 cart, 2 purchase" },
    { name = "product_id", type = "INTEGER", mode = "NULLABLE" },
    { name = "category_id", type = "INTEGER", mode = "NULLABLE" },
    { name = "category_code", type = "STRING", mode = "NULLABLE" },
    { name = "brand", type = "STRING", mode = "NULLABLE" },
    { name = "price", type = "FLOAT", mode = "NULLABLE" },
    { name = "user_id", type = "INTEGER", mode = "NULLABLE" },
    { name = "session", type = "INTEGER", mode = "NULLABLE", description = "Dense id, in order of each session's first event" },
    { name = "seq", type = "INTEGER", mode = "NULLABLE", description = "Position in the source file, which is in time order: orders events within a second" },
  ])
}

resource "google_bigquery_table" "covis" {
  dataset_id          = google_bigquery_dataset.recs.dataset_id
  table_id            = "covis"
  description         = "Co-visitation: each product's 20 strongest neighbours, per matrix and night"
  deletion_protection = false
  clustering          = ["kind", "product_id"]

  time_partitioning {
    type  = "DAY"
    field = "version"
  }

  schema = jsonencode([
    { name = "version", type = "DATE", mode = "NULLABLE", description = "The last day of events the tables include" },
    { name = "kind", type = "STRING", mode = "NULLABLE", description = "time, type or buy2buy" },
    { name = "product_id", type = "INTEGER", mode = "NULLABLE" },
    { name = "neighbour", type = "INTEGER", mode = "NULLABLE" },
    { name = "weight", type = "FLOAT", mode = "NULLABLE" },
    { name = "rank", type = "INTEGER", mode = "NULLABLE", description = "1 is the strongest" },
  ])
}

resource "google_bigquery_table" "product_stats" {
  dataset_id          = google_bigquery_dataset.recs.dataset_id
  table_id            = "product_stats"
  description         = "Per product and night: recent activity, and the latest price, category and brand"
  deletion_protection = false
  clustering          = ["product_id"]

  time_partitioning {
    type  = "DAY"
    field = "version"
  }

  schema = jsonencode([
    { name = "version", type = "DATE", mode = "NULLABLE", description = "The last day of events the tables include" },
    { name = "product_id", type = "INTEGER", mode = "NULLABLE" },
    { name = "p_events_1d", type = "INTEGER", mode = "NULLABLE" },
    { name = "p_events_7d", type = "INTEGER", mode = "NULLABLE" },
    { name = "p_carts_7d", type = "INTEGER", mode = "NULLABLE" },
    { name = "p_purchases_7d", type = "INTEGER", mode = "NULLABLE" },
    { name = "p_price", type = "FLOAT", mode = "NULLABLE" },
    { name = "p_category", type = "INTEGER", mode = "NULLABLE" },
    { name = "p_brand", type = "STRING", mode = "NULLABLE" },
  ])
}
