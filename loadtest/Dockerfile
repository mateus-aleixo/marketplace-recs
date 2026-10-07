# k6 with the scripts and the request bodies, to run as a Job inside the cluster. The
# bodies are real test sessions (python -m marketplace_recs.loadtest), so the image stays
# in the project's private registry and the file is never committed.
#
#   docker build -t <registry>/k6:<tag> loadtest
FROM grafana/k6:2.3.0
COPY cluster.js recommend.js sessions.json /scripts/
WORKDIR /scripts
