# OpenSearch Index Comparison Tool

Compares two OpenSearch indices and writes document IDs that exist in the source but are missing from the target. Uses the Scroll API for efficient processing of large indices.

Works against:

- **Amazon OpenSearch Service** managed domains via IAM/SigV4 or HTTP basic auth (fine-grained access control)
- **Amazon OpenSearch Serverless** collections via IAM/SigV4
- Self-managed OpenSearch with HTTP basic auth
- A local OpenSearch container with security disabled (see [docker-compose.yml](docker-compose.yml))

## Features

- AOS-friendly auth modes: SigV4 (`aws-iam`, supports both managed `es` and serverless `aoss`), HTTP basic (`basic`), or none
- Configuration via `.env` file, environment variables, or CLI flags
- Scroll API for large indices, with configurable batch size and context lifetime
- Detailed progress logging

## Requirements

- Python 3.9+
- `opensearch-py`, `boto3`, `python-dotenv` (see [requirements.txt](requirements.txt))
- Docker + Docker Compose v2 (only for the local test cluster)

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Configuration is layered. Highest precedence first:

1. CLI flags
2. Process environment variables
3. `.env` file in the working directory (or via `--env-file`)
4. Built-in defaults

Copy `.env.example` to `.env` and edit:

```bash
cp .env.example .env
```

Key variables:

| Variable | CLI flag | Description |
| --- | --- | --- |
| `OS_AUTH_MODE` | `--auth-mode` | `aws-iam`, `basic`, or `none` |
| `OS_HOST` | `--host` | Endpoint host (no scheme) |
| `OS_PORT` | `--port` | Default `9200`. Use `443` for AOS; the local docker-compose maps to `9201`. |
| `OS_SOURCE_INDEX` | `--source` | Source index name |
| `OS_TARGET_INDEX` | `--target` | Target index name |
| `OS_OUTPUT_FILE` | `--output` | Output file for missing IDs |
| `OS_USE_SSL` | `--use-ssl` / `--no-use-ssl` | Force HTTPS on/off. Auto-detected from host: forced on for `aws-iam`, on for non-local hosts otherwise. |
| `OS_VERIFY_CERTS` | `--verify-certs` / `--no-verify-certs` | Verify TLS certs. Defaults to true for non-local hosts. |
| `OS_AWS_REGION` / `AWS_REGION` | `--aws-region` | Region for SigV4 signing |
| `OS_AWS_SERVICE` | `--aws-service` | `es` (managed AOS, default) or `aoss` (OpenSearch Serverless) |
| `OS_USER` | `--user` | Basic auth username |
| `OS_PASSWORD` | `--password` / `--password-stdin` | Basic auth password (see warning below) |
| `OS_SCROLL_SIZE` | `--scroll-size` | Documents per scroll batch (default `1000`) |
| `OS_SCROLL_TIME` | `--scroll-time` | Scroll context lifetime (default `5m`) |

> **Credentials handling.** AWS credentials for `aws-iam` mode are picked up from the boto3 default chain (env vars, `~/.aws/credentials`, instance/task role, SSO). Do **not** put them in `.env`.
>
> **Password handling.** Passing a password via `--password` causes it to appear in process listings (`ps`) and shell history. Prefer `OS_PASSWORD` in `.env`, or pipe via `--password-stdin`:
>
> ```bash
> printf '%s' "$MY_PASSWORD" | python compare_indices.py --auth-mode basic --password-stdin ...
> ```

## Usage

### Amazon OpenSearch Service managed domain (IAM / SigV4)

The IAM principal running the tool needs read access on the domain. Minimum policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "es:ESHttpGet",
        "es:ESHttpPost",
        "es:ESHttpHead"
      ],
      "Resource": "arn:aws:es:REGION:ACCOUNT_ID:domain/DOMAIN_NAME/*"
    }
  ]
}
```

If the domain has fine-grained access control enabled, also map this IAM principal to a backend role (e.g. `readall` or a custom role) in OpenSearch Dashboards → Security → Roles.

```bash
python compare_indices.py \
  --auth-mode aws-iam \
  --host search-mydomain-abc123.us-east-1.es.amazonaws.com \
  --port 443 \
  --aws-region us-east-1 \
  --source products \
  --target products_backup \
  --output missing_ids.txt
```

Or put it all in `.env` and run:

```bash
python compare_indices.py
```

### Amazon OpenSearch Serverless collection (IAM / SigV4)

Set `--aws-service aoss`. The principal needs an [OpenSearch Serverless data access policy](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-data-access.html) granting `aoss:ReadDocument` on the collection's indices, plus the network policy must allow access from where the tool runs.

```bash
python compare_indices.py \
  --auth-mode aws-iam --aws-service aoss \
  --host abc123xyz.us-east-1.aoss.amazonaws.com --port 443 \
  --aws-region us-east-1 \
  --source products --target products_backup \
  --output missing_ids.txt
```

### AOS managed domain with fine-grained access control (basic auth)

```bash
export OS_PASSWORD='...'   # do not pass on CLI
python compare_indices.py \
  --auth-mode basic \
  --host search-mydomain-abc123.us-east-1.es.amazonaws.com --port 443 \
  --user admin \
  --source products --target products_backup --output missing_ids.txt
```

### Local docker-compose cluster (no auth)

```bash
docker compose up -d --wait
python compare_indices.py \
  --auth-mode none --host localhost --port 9201 \
  --source test_source --target test_target --output missing_ids.txt
```

End-to-end smoke tests are scripted in [run_test.sh](run_test.sh) and [run_large_test.sh](run_large_test.sh).

## Output

A text file with one document ID per line — IDs present in the source index but missing from the target index, sorted lexicographically.

## Cleanup

To stop and remove the local OpenSearch test container and its data volume:

```bash
docker compose down -v
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `403 Forbidden` from a managed AOS domain | IAM policy missing `es:ESHttp*`, or fine-grained access control role not mapped to your IAM principal. |
| `403 Forbidden` from a Serverless collection | Missing `aoss:ReadDocument` data access policy, network policy doesn't allow your source IP/VPC, or `--aws-service es` used by mistake (must be `aoss`). |
| `AuthenticationException` with basic auth | Wrong username/password, or fine-grained access control is not enabled on the domain. |
| `Failed to connect to OpenSearch` against AOS | Forgot `--port 443`, or pointed at HTTP when HTTPS is required. The tool auto-enables SSL for non-local hosts; verify with `--use-ssl`. |
| `Search failed ... scroll context may have expired` | Index is large and scroll context (`--scroll-time`) timed out between batches. Increase `--scroll-time` (e.g. `10m`) or reduce `--scroll-size`. |
| Local container doesn't start; `bootstrap.memory_lock=true` warnings | Docker Desktop on macOS doesn't support `memlock`; the warning is benign. |

## Pagination notes

This sample uses the **Scroll API** for compatibility with older clusters. For new code on **OpenSearch 2.4+** (managed) consider [Point-in-Time + search_after](https://opensearch.org/docs/latest/search-plugins/searching-data/point-in-time/) instead — it avoids server-side scroll context state and scales better for very large result sets. OpenSearch Serverless does not support the Scroll API; use PIT + search_after there (this tool does not currently target Serverless for that reason — track the limitation when planning a comparison against a Serverless collection).

## Cost

This tool reads from existing OpenSearch indices and provisions no AWS resources of its own. It incurs no AWS charges beyond the read-side load on whatever domain or collection you point it at.

## Security

Report security issues per the [repo CONTRIBUTING guide](../../CONTRIBUTING.md#security-issue-notifications). Do not commit secrets — `.env` is gitignored at the repo root.
