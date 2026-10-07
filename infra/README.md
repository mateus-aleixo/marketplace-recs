# Infrastructure

Two Terraform stacks on Google Cloud, applied from a laptop signed in with
`gcloud auth login --update-adc`. State and the real variables stay on that machine,
out of git.

| stack | what it holds | lifetime |
|---|---|---|
| [`core`](core) | the project and its APIs, the budget, the image registry, BigQuery, Cloud Run | stays up, and costs nothing at rest |
| [`gke`](gke) | an Autopilot cluster running the API behind a service and an autoscaler | only while a load test runs |

`gke` reads the project and the image from `core`'s state, so `core` goes first.

## core

```bash
cd infra/core
cp terraform.tfvars.example terraform.tfvars   # a new project id, and the billing account
terraform init && terraform apply              # project, APIs, budget, registry, BigQuery

# The image carries the model that `python -m marketplace_recs.online export` wrote.
gcloud auth configure-docker europe-west1-docker.pkg.dev
IMAGE=$(terraform output -raw registry)/api:$(git rev-parse --short HEAD)
docker build -t $IMAGE ../.. && docker push $IMAGE
# Put its digest in terraform.tfvars as api_image; the next apply creates the service.
terraform apply && terraform output api_url
```

**Budget.** One budget watches the whole billing account and emails its administrators
at €5 and €10 of spend. The free trial pays through a credit of type `PROMOTION`,
and a budget that subtracts it sees no spend until the trial runs out, so it subtracts
every credit type except that one. It counts from 1 October 2026 with no end date
instead of starting again each month. A budget only alerts, and cost data arrives
hours late.

**Cloud Run.** Scales to zero, at most `api_max_instances` instances (2), CPU only while
a request is in flight. The public endpoint is the stateless `POST /recommend`: session
state would need Redis, which a free demo does not pay for.

**BigQuery.** The dataset `recs`: `events` (partitioned by day, clustered by product)
and the nightly tables `covis` and `product_stats` (one partition per night). Loading
and the nightly SQL are in `marketplace_recs.warehouse`:

```bash
export GOOGLE_CLOUD_PROJECT=$(terraform -chdir=infra/core output -raw project_id)
python -m marketplace_recs.warehouse load                        # 42M events, ~1 minute
python -m marketplace_recs.warehouse parity 2019-10-23           # SQL against polars
python -m marketplace_recs.pipeline 2019-10-27 --warehouse bigquery
```

## gke

```bash
terraform -chdir=infra/gke apply                  # about 10 minutes
$(terraform -chdir=infra/gke output -raw credentials)

K6=$(terraform -chdir=infra/core output -raw registry)/k6:2
docker build -t $K6 loadtest && docker push $K6   # needs loadtest/sessions.json
python -m marketplace_recs.cluster --label ramp --image $K6 --cpu 3 \
    --rates 100,200,300,400,500,600,700 --step 90 --new-connections

# A fixed number of pods: pin the autoscaler, then load it.
terraform -chdir=infra/gke apply -var min_pods=4 -var max_pods=4
python -m marketplace_recs.cluster --label four-pods --image $K6 --cpu 3 \
    --rates 400,600,720,840 --step 60 --new-connections

terraform -chdir=infra/gke destroy                # every time
```

Nodes run as their own service account with `roles/container.defaultNodeServiceAccount`
and read access to the registry, not as the Compute Engine default account. k6 runs as a
Job inside the cluster, so the test measures the service rather than a path over the
internet.

**Free trial quotas.** A trial project gets 12 vCPUs across all regions and 250 GB of
SSD, and cannot ask for more. Every Autopilot node boots from a 100 GB SSD, so the
cluster holds two nodes at most. Here Autopilot chose an `ek-standard-8` and an
`e2-standard-2`, and the third node it asked for failed with "GCE quota exceeded": seven
API pods at most, five beside a k6 Job of three vCPUs, hence `max_pods = 5`.
