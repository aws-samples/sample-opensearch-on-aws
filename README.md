# Amazon OpenSearch Samples on AWS

This repository provides sample code and reference architectures for building production-ready solutions with **Amazon OpenSearch Service**. Samples cover ingestion, search relevance, vector search, observability, operational tooling, and integration patterns — each one self-contained with its own README, deployment steps, and security guidance.

## 🗒️ Table of Contents
- [Samples](#-samples)
- [How samples are organized](#-how-samples-are-organized)
- [AWS Blogs](#-aws-blogs)
- [Contributing](#-contributing)
- [Security](#-security)
- [License](#-license)

## 📑 Samples

| Sample | Description |
|---|---|
| [ingestion](ingestion/) | Load and reload data into Amazon OpenSearch Service using OpenSearch Ingestion (OSI) pipelines. Includes a full AWS CDK stack with VPC, OpenSearch domain, OSI pipeline, S3 buckets, and SSM-accessible jumphost. |

_More samples coming soon — vector search patterns, search relevance tuning, and operational tooling._

## 📂 How samples are organized

Each sample lives in its own top-level directory and is fully self-contained:

- Its own `README.md` with prerequisites, deployment, and cleanup steps
- Its own infrastructure code (CDK, CloudFormation, or Terraform) where applicable
- Its own `THREAT_MODEL.md` for samples that provision AWS resources
- Its own dependency manifest (`requirements.txt`, `package.json`, etc.)

You can clone the whole repo and work on one sample without building the others.

```
sample-opensearch-on-aws/
├── README.md                 # This file — index of all samples
├── LICENSE                   # MIT-0
├── NOTICE
├── CODE_OF_CONDUCT.md
├── CONTRIBUTING.md
└── ingestion/                # First sample
    ├── README.md
    ├── THREAT_MODEL.md
    └── ...
```

## 💡 AWS Blogs

_Links to related AWS blog posts will be added as samples are published._

## 🙌 Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on proposing new samples, reporting issues, and submitting pull requests.

## 🔒 Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information on reporting security issues.

## 📄 License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
