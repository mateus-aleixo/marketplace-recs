# Alerts by email at EUR 5 and EUR 10 of spend, to the billing account's administrators.
#
# Three choices differ from a default budget:
#   - The free trial credit is a PROMOTION credit. A budget that subtracts it sees no spend
#     until the trial runs out, so every credit type except that one is subtracted, and the
#     alerts fire on what the trial is paying for.
#   - The period has a start and no end: spend accumulates from October 2026 instead of
#     starting again each month, so a slow leak over several months still reaches EUR 10.
#   - It watches the whole billing account, not only this project.
# A budget alerts; it does not stop anything. Cost data also arrives hours late.
resource "google_billing_budget" "guard" {
  provider        = google.billing
  billing_account = var.billing_account
  display_name    = "Guard: EUR 5 and EUR 10"

  budget_filter {
    credit_types_treatment = "INCLUDE_SPECIFIED_CREDITS"
    credit_types = [
      "COMMITTED_USAGE_DISCOUNT",
      "COMMITTED_USAGE_DISCOUNT_DOLLAR_BASE",
      "DISCOUNT",
      "FREE_TIER",
      "SUBSCRIPTION_BENEFIT",
      "SUSTAINED_USAGE_DISCOUNT",
    ]
    custom_period {
      start_date {
        year  = 2026
        month = 10
        day   = 1
      }
    }
  }

  amount {
    specified_amount {
      currency_code = "EUR"
      units         = "10"
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 1.0
  }

  depends_on = [google_project_service.api["billingbudgets.googleapis.com"]]
}
