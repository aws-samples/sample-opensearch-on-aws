# Threat Model: OpenSearch Ingestion Data Loading Solution

## 1. System Overview

This solution deploys an AWS CDK stack for loading data into Amazon OpenSearch Service via OpenSearch Ingestion (OSI). The architecture includes:

- VPC with public/private subnets (3 AZs)
- Amazon OpenSearch Service domain (VPC-deployed, 3 data + 3 dedicated master nodes)
- OpenSearch Ingestion pipeline
- S3 buckets (source and DLQ)
- EC2 jumphost with nginx reverse proxy (SSM-managed, no SSH)
- Lambda function for FGAC configuration (custom resource)
- Secrets Manager for credential storage and generation
- CloudWatch log groups for OpenSearch slow logs and OSI pipeline logs

## 2. Data Flow Diagram

```
[User/Data Source] → [S3 Source Bucket] → [OSI Pipeline] → [OpenSearch Domain]
                                                              ↓
[Admin] → [SSM Session Manager] → [Jumphost/Nginx] → [OpenSearch Dashboards]
                                                         [DLQ Bucket]
```

## 3. Trust Boundaries

| Boundary | Components | Trust Level |
|----------|------------|-------------|
| Internet | External users, HTTPS access to jumphost | Untrusted |
| VPC Public Subnet | Jumphost EC2 (SSM-managed) | Semi-trusted |
| VPC Private Subnet | OpenSearch, Lambda, OSI Pipeline | Trusted |
| AWS Services | S3, Secrets Manager, IAM, CloudWatch | Trusted |

## 4. Threat Analysis (STRIDE)

### 4.1 Spoofing

| ID | Threat | Component | Risk | Mitigation Status |
|----|--------|-----------|------|-------------------|
| S1 | Unauthorized OpenSearch access | OpenSearch Domain | HIGH | ✅ MITIGATED - Access policy scoped to OSI role ARN; FGAC enabled; domain in private subnet with SG restricting to VPC CIDR |
| S2 | Credential theft from CloudFormation outputs | Stack Outputs | HIGH | ✅ FIXED - Password stored in Secrets Manager; output provides Secret ARN only |
| S3 | Jumphost impersonation | EC2 Instance | MEDIUM | ✅ MITIGATED - Security group restricts HTTPS; SSH removed; SSM Session Manager for admin access |

### 4.2 Tampering

| ID | Threat | Component | Risk | Mitigation Status |
|----|--------|-----------|------|-------------------|
| T1 | S3 data modification | Source/DLQ Buckets | MEDIUM | ⚠️ PARTIAL - SSL enforced, public access blocked, but no versioning enabled |
| T2 | Pipeline configuration tampering | OSI Pipeline | LOW | ✅ IAM controls access |
| T3 | Index mapping manipulation | OpenSearch | MEDIUM | ✅ FGAC controls write access via backend role mapping |

### 4.3 Repudiation

| ID | Threat | Component | Risk | Mitigation Status |
|----|--------|-----------|------|-------------------|
| R1 | Untracked data modifications | OpenSearch | LOW | ✅ MITIGATED - CloudWatch logs enabled for OSI pipeline; slow search and slow index logs enabled |
| R2 | S3 access without audit trail | S3 Buckets | MEDIUM | ⚠️ KNOWN - No S3 access logging (recursive logging issue); use CloudTrail S3 data events for production |

### 4.4 Information Disclosure

| ID | Threat | Component | Risk | Mitigation Status |
|----|--------|-----------|------|-------------------|
| I1 | Credentials in plaintext outputs | CFN Outputs | CRITICAL | ✅ FIXED - Only Secret ARN exposed; credentials retrieved via Secrets Manager API |
| I2 | Data exposure via jumphost | Nginx Proxy | MEDIUM | ⚠️ PARTIAL - Self-signed cert; HTTPS configurable to specific CIDR; nginx verifies upstream SSL |
| I3 | Secrets Manager access | Master credentials | LOW | ✅ FIXED - Scoped to `osi-load-*` prefix with `secretsmanager:GetSecretValue` only |
| I4 | SSL/TLS certificate validation disabled | OpenSearch client | MEDIUM | ⚠️ KNOWN - `verify_certs=False` in shared_utils.py and FGAC Lambda for self-signed proxy cert |

### 4.5 Denial of Service

| ID | Threat | Component | Risk | Mitigation Status |
|----|--------|-----------|------|-------------------|
| D1 | S3 bucket exhaustion | Source Bucket | LOW | ✅ OSI has rate limiting |
| D2 | OpenSearch cluster overload | OpenSearch Domain | LOW | ✅ MITIGATED - OSI max_units=4 limits throughput; 3-node cluster with dedicated masters |
| D3 | Jumphost resource exhaustion | EC2 Instance | LOW | ✅ t3.small limits impact |

### 4.6 Elevation of Privilege

| ID | Threat | Component | Risk | Mitigation Status |
|----|--------|-----------|------|-------------------|
| E1 | OSI role over-permissioned | IAM Role | MEDIUM | ✅ FIXED - S3 scoped to specific buckets; OpenSearch scoped to specific domain ARN; Secrets Manager scoped to `osi-load-*` prefix |
| E2 | Lambda role escalation | FGAC Lambda | MEDIUM | ⚠️ PARTIAL - Shares OSI role (required for FGAC backend role mapping); has `AWSLambdaVPCAccessExecutionRole` managed policy |
| E3 | Jumphost to internal network | EC2 Instance | LOW | ✅ MITIGATED - Security groups limit lateral movement; jumphost role has only SSM managed policy |

## 5. Vulnerability Status

### 5.1 [CRITICAL] Credentials Exposed in CloudFormation Outputs

**Location:** `osi_load/osi_load_stack.py` - CfnOutput section

**Status:** ✅ FIXED

**Fix Applied:**
- Removed plaintext password from CFN outputs
- Using Secrets Manager `generate_secret_string` for secure 16-character password generation
- Output provides Secret ARN for retrieval via `aws secretsmanager get-secret-value`
- Password excludes characters that could cause shell escaping issues (`"@/\`)

### 5.2 [HIGH] Overly Permissive IAM Policies

**Location:** `osi_load/osi_load_stack.py` `_create_osi_os_role` method

**Status:** ✅ FIXED

**Fix Applied:**
- S3 permissions scoped to specific source and DLQ bucket ARNs
- OpenSearch permissions scoped to specific domain ARN with enumerated actions (`ESHttpPost`, `ESHttpPut`, `ESHttpGet`, `ESHttpDelete`, `ESHttpHead`, `DescribeDomain`, `DescribeDomains`)
- Secrets Manager permissions scoped to `osi-load-*` prefix with `GetSecretValue` only

### 5.3 [HIGH] OpenSearch Access Policy Too Permissive

**Location:** `osi_load/osi_load_stack.py` access_policies

**Status:** ✅ FIXED

**Fix Applied:**
- Changed from `AnyPrincipal()` to `ArnPrincipal(osi_os_role.role_arn)`
- Reduced actions from `es:*` to `es:ESHttp*`
- Resources scoped to specific domain path

**Note:** When `use_unsigned_basic_auth=True` is set, CDK adds a second access policy statement with `Principal: "*"` to the synthesized CloudFormation template. This is required for FGAC basic authentication to function. The domain is deployed in private VPC subnets with security groups restricting access to the VPC CIDR block only, so this open principal cannot be reached from the internet. See the [OpenSearch FGAC documentation](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/fgac.html) for details.

### 5.4 [HIGH] Jumphost SSH Open to Internet

**Location:** `osi_load/osi_load_stack.py` security group rules

**Status:** ✅ FIXED

**Fix Applied:**
- Removed SSH (port 22) ingress rule entirely
- Added SSM Session Manager support via `AmazonSSMManagedInstanceCore` managed policy
- Added instance ID output for SSM access
- HTTPS configurable: restrict with `cdk deploy -c jumphost_allowed_cidr=YOUR_IP/32`

### 5.5 [MEDIUM] Self-Signed Certificate with No Validation

**Location:** `shared_utils.py` line 42, FGAC Lambda inline code
```python
verify_certs=False  # Proxy uses self-signed cert
```

**Status:** ⚠️ KNOWN LIMITATION

**Impact:** Susceptible to man-in-the-middle attacks on the proxy-to-client path.

**Mitigations in place:**
- Nginx upstream connections to OpenSearch use `proxy_ssl_verify on` with system CA bundle
- OpenSearch domain enforces HTTPS with node-to-node encryption
- Domain is in a private VPC subnet, limiting attack surface

**Recommendation:** Use ACM certificate or implement certificate pinning for production.

## 6. Security Controls Summary

### Encryption
| Control | Status | Details |
|---------|--------|---------|
| S3 SSL enforcement | ✅ Enabled | `enforce_ssl=True` on both buckets |
| S3 public access block | ✅ Enabled | `BlockPublicAccess.BLOCK_ALL` on both buckets |
| OpenSearch HTTPS | ✅ Enforced | `enforce_https=True` |
| OpenSearch node-to-node encryption | ✅ Enabled | `node_to_node_encryption=True` |
| OpenSearch encryption at rest | ✅ Enabled | `encryption_at_rest=True` |
| EC2 EBS encryption | ✅ Enabled | Root volume encrypted |
| Nginx TLS | ✅ Enabled | TLSv1.2/TLSv1.3 with strong ciphers; upstream SSL verification enabled |

### Access Control
| Control | Status | Details |
|---------|--------|---------|
| OpenSearch FGAC | ✅ Enabled | Backend role mapping via custom Lambda |
| IAM least privilege | ✅ Scoped | All policies scoped to specific resources |
| Security groups | ✅ Configured | OpenSearch: VPC CIDR only; Jumphost: HTTPS only (configurable CIDR) |
| SSM Session Manager | ✅ Enabled | Replaces SSH for jumphost access |
| S3 bucket policies | ✅ SSL-only | Enforced via `enforce_ssl` |

### Logging & Monitoring
| Control | Status | Details |
|---------|--------|---------|
| OpenSearch slow search logs | ✅ Enabled | Dedicated CloudWatch log group |
| OpenSearch slow index logs | ✅ Enabled | Dedicated CloudWatch log group |
| OSI pipeline logs | ✅ Enabled | CloudWatch log group with `is_logging_enabled=True` |
| VPC Flow Logs | ❌ Not enabled | Suppressed via cdk-nag; recommended for production |
| S3 access logging | ❌ Not enabled | Recursive logging issue; use CloudTrail data events for production |
| EC2 detailed monitoring | ⚙️ Optional | Enable with `cdk deploy -c enable_detailed_monitoring=true` |

### CDK-Nag Compliance
The stack includes cdk-nag suppressions with documented justifications for all findings. Key suppressions:
- `AwsSolutions-VPC7`: VPC Flow Logs (sample code scope)
- `AwsSolutions-S1`: S3 access logging (recursive logging issue)
- `AwsSolutions-EC28`/`EC29`: Detailed monitoring and termination protection (configurable/sample scope)
- `AwsSolutions-OS5`: Unsigned basic auth (required for FGAC)
- `AwsSolutions-SMG4`: Secret rotation (documented recommendation)

## 7. Security Recommendations

### Immediate Actions (P0) - ✅ ALL COMPLETED

1. ✅ **Remove password from CFN outputs** - Now stored only in Secrets Manager
2. ✅ **Scope IAM policies** - All resources scoped to specific ARNs/prefixes
3. ✅ **Restrict OpenSearch access policy** - Limited to OSI role principal only
4. ✅ **Restrict jumphost access** - Removed SSH, use SSM Session Manager

### Short-term Actions (P1)

5. **Enable S3 access logging** - Use CloudTrail S3 data events (see Production Hardening in README)
6. **Enable S3 versioning** - For data integrity protection
7. **Use ACM certificate** - Replace self-signed cert on jumphost
8. **Enable VPC Flow Logs** - For network monitoring
9. ~~**Enable OpenSearch slow logs**~~ - ✅ Now enabled by default (slow search + slow index)

### Long-term Actions (P2)

10. **Separate IAM roles** - Dedicated roles for OSI, Lambda, and admin access
11. **Add WAF** - Protect jumphost from web attacks
12. **Enable CloudTrail** - For comprehensive audit logging
13. **Implement secrets rotation** - Auto-rotate OpenSearch credentials (see Production Hardening in README)
14. **KMS CMK encryption** - Replace default SSE-S3 with customer-managed keys on S3 buckets

## 8. Compliance Considerations

| Framework | Relevant Controls | Status |
|-----------|------------------|--------|
| AWS Well-Architected | SEC01-SEC11 | Mostly addressed (encryption, IAM, network controls in place) |
| SOC 2 | CC6.1, CC6.6, CC6.7 | Gaps: S3 access logging, VPC Flow Logs |
| GDPR | Art. 32 (Security) | Review needed for PII handling |

## 9. Attack Scenarios

### Scenario 1: Credential Compromise (Mitigated)
1. ~~Attacker gains CloudFormation read access~~
2. ~~Retrieves master password from stack outputs~~
3. Stack outputs only expose Secret ARN, not credentials
4. Attacker would need both CFN read access AND Secrets Manager access scoped to `osi-load-*`
5. **Residual risk:** Attacker with sufficient IAM permissions could still retrieve credentials

### Scenario 2: Lateral Movement (Mitigated)
1. ~~Attacker compromises jumphost via SSH~~
2. SSH is disabled; jumphost accessible only via SSM Session Manager (requires IAM auth)
3. Jumphost role has only SSM managed policy, no OpenSearch or S3 permissions
4. Security groups restrict OpenSearch access to VPC CIDR
5. **Residual risk:** Attacker with SSM access could use nginx proxy to reach OpenSearch

### Scenario 3: Data Poisoning
1. Attacker gains S3 write access to source bucket
2. Uploads malicious documents
3. OSI pipeline indexes poisoned data
4. Search results compromised
5. **Mitigations:** S3 SSL enforcement, public access blocked, IAM scoped to specific role

## 10. Residual Risk Assessment

| Risk Category | Initial Level | Current Level | Target Level | Notes |
|---------------|---------------|---------------|--------------|-------|
| Authentication | HIGH | LOW | LOW | Secrets Manager, FGAC, SSM Session Manager |
| Authorization | HIGH | LOW | LOW | Scoped IAM policies, FGAC backend roles |
| Data Protection | MEDIUM | LOW-MEDIUM | LOW | Encryption at rest/transit enabled; no S3 versioning |
| Logging/Monitoring | MEDIUM | LOW-MEDIUM | LOW | Slow logs + OSI logs enabled; no VPC Flow Logs or S3 access logs |
| Network Security | HIGH | LOW | LOW | SSH removed, SGs configured, private subnets, SSM access |

## 11. Document Information

- **Version:** 2.0
- **Date:** May 4, 2026
- **Author:** Security Review
- **Review Cycle:** Quarterly or after significant changes
- **Previous Version:** 1.1 (March 30, 2026)

### Change Log
| Version | Date | Changes |
|---------|------|---------|
| 2.0 | May 4, 2026 | Updated all STRIDE findings to reflect current implementation; added Security Controls Summary section; updated residual risk assessment; revised attack scenarios with current mitigations; added CDK-nag compliance details; updated document structure |
| 1.1 | March 30, 2026 | Documented fixes for P0 vulnerabilities |
| 1.0 | Initial | Initial threat model |
