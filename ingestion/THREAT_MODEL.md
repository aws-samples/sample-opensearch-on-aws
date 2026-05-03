# Threat Model: OpenSearch Ingestion Data Loading Solution

## 1. System Overview

This solution deploys an AWS CDK stack for loading data into Amazon OpenSearch Service via OpenSearch Ingestion (OSI). The architecture includes:

- VPC with public/private subnets
- Amazon OpenSearch Service domain (VPC-deployed)
- OpenSearch Ingestion pipeline
- S3 buckets (source and DLQ)
- EC2 jumphost with nginx reverse proxy
- Lambda function for FGAC configuration
- Secrets Manager for credentials

## 2. Data Flow Diagram

```
[User/Data Source] → [S3 Source Bucket] → [OSI Pipeline] → [OpenSearch Domain]
                                                              ↓
[Admin] → [Jumphost/Nginx] → [OpenSearch Dashboards]    [DLQ Bucket]
```

## 3. Trust Boundaries

| Boundary | Components | Trust Level |
|----------|------------|-------------|
| Internet | External users, SSH/HTTPS access | Untrusted |
| VPC Public Subnet | Jumphost EC2 | Semi-trusted |
| VPC Private Subnet | OpenSearch, Lambda, OSI | Trusted |
| AWS Services | S3, Secrets Manager, IAM | Trusted |

## 4. Threat Analysis (STRIDE)

### 4.1 Spoofing

| ID | Threat | Component | Risk | Mitigation Status |
|----|--------|-----------|------|-------------------|
| S1 | Unauthorized OpenSearch access | OpenSearch Domain | HIGH | ⚠️ PARTIAL - FGAC enabled but access policy allows AnyPrincipal |
| S2 | Credential theft from CloudFormation outputs | Stack Outputs | HIGH | ❌ VULNERABLE - Password exposed in CFN outputs |
| S3 | Jumphost impersonation | EC2 Instance | MEDIUM | ✅ Security group restricts access |

### 4.2 Tampering

| ID | Threat | Component | Risk | Mitigation Status |
|----|--------|-----------|------|-------------------|
| T1 | S3 data modification | Source/DLQ Buckets | MEDIUM | ⚠️ PARTIAL - No versioning enabled |
| T2 | Pipeline configuration tampering | OSI Pipeline | LOW | ✅ IAM controls access |
| T3 | Index mapping manipulation | OpenSearch | MEDIUM | ✅ FGAC controls write access |

### 4.3 Repudiation

| ID | Threat | Component | Risk | Mitigation Status |
|----|--------|-----------|------|-------------------|
| R1 | Untracked data modifications | OpenSearch | MEDIUM | ⚠️ PARTIAL - CloudWatch logs enabled for OSI |
| R2 | S3 access without audit trail | S3 Buckets | MEDIUM | ❌ VULNERABLE - No S3 access logging |

### 4.4 Information Disclosure

| ID | Threat | Component | Risk | Mitigation Status |
|----|--------|-----------|------|-------------------|
| I1 | Credentials in plaintext outputs | CFN Outputs | CRITICAL | ❌ VULNERABLE - Password in OsiLoadMasterPassword output |
| I2 | Data exposure via jumphost | Nginx Proxy | HIGH | ⚠️ PARTIAL - Self-signed cert, no client auth |
| I3 | Secrets Manager access | Master credentials | MEDIUM | ⚠️ PARTIAL - Wildcard resource in IAM policy |
| I4 | SSL/TLS certificate validation disabled | OpenSearch client | MEDIUM | ⚠️ KNOWN - verify_certs=False for self-signed proxy |

### 4.5 Denial of Service

| ID | Threat | Component | Risk | Mitigation Status |
|----|--------|-----------|------|-------------------|
| D1 | S3 bucket exhaustion | Source Bucket | LOW | ✅ OSI has rate limiting |
| D2 | OpenSearch cluster overload | OpenSearch Domain | MEDIUM | ⚠️ PARTIAL - OSI max_units=4 limits throughput |
| D3 | Jumphost resource exhaustion | EC2 Instance | LOW | ✅ t3.small limits impact |

### 4.6 Elevation of Privilege

| ID | Threat | Component | Risk | Mitigation Status |
|----|--------|-----------|------|-------------------|
| E1 | OSI role over-permissioned | IAM Role | HIGH | ❌ VULNERABLE - Wildcard resources for es:* and secretsmanager |
| E2 | Lambda role escalation | FGAC Lambda | MEDIUM | ⚠️ PARTIAL - Uses shared OSI role |
| E3 | Jumphost to internal network | EC2 Instance | MEDIUM | ✅ Security groups limit lateral movement |

## 5. Critical Vulnerabilities

### 5.1 [CRITICAL] Credentials Exposed in CloudFormation Outputs

**Location:** `osi_load/osi_load_stack.py` lines 195-197

**Status:** ✅ FIXED

**Fix Applied:** 
- Removed plaintext password from CFN outputs
- Now using Secrets Manager's `generate_secret_string` for secure password generation
- Output now provides Secret ARN for retrieval via AWS CLI/SDK

### 5.2 [HIGH] Overly Permissive IAM Policies

**Location:** `osi_load/osi_load_stack.py` `_create_osi_os_role` method

**Status:** ✅ FIXED

**Fix Applied:**
- OpenSearch permissions scoped to specific domain ARN
- Secrets Manager permissions scoped to `osi-load-*` prefix
- S3 permissions already properly scoped

### 5.3 [HIGH] OpenSearch Access Policy Too Permissive

**Location:** `osi_load/osi_load_stack.py` access_policies

**Status:** ✅ FIXED

**Fix Applied:**
- Changed from `AnyPrincipal()` to `ArnPrincipal(osi_os_role.role_arn)`
- Reduced actions from `es:*` to `es:ESHttp*`

### 5.4 [HIGH] Jumphost SSH Open to Internet

**Location:** `osi_load/osi_load_stack.py` security group rules

**Status:** ✅ FIXED

**Fix Applied:**
- Removed SSH (port 22) ingress rule entirely
- Added SSM Session Manager support via existing `AmazonSSMManagedInstanceCore` policy
- Added instance ID output for SSM access
- HTTPS open by default; restrict with `cdk deploy -c jumphost_allowed_cidr=YOUR_IP/32`

### 5.5 [MEDIUM] Self-Signed Certificate with No Validation

**Location:** `shared_utils.py` line 19, `osi_load/osi_load_stack.py` (Lambda code)
```python
verify_certs=False  # Proxy may use self-signed cert
```

**Impact:** Susceptible to man-in-the-middle attacks.

**Recommendation:** Use ACM certificate or implement certificate pinning.

## 6. Security Recommendations

### Immediate Actions (P0) - ✅ COMPLETED

1. ✅ **Remove password from CFN outputs** - Now stored only in Secrets Manager
2. ✅ **Scope IAM policies** - Replaced wildcard resources with specific ARNs
3. ✅ **Restrict OpenSearch access policy** - Limited to OSI role principal only
4. ✅ **Restrict jumphost access** - Removed SSH, use SSM Session Manager

### Short-term Actions (P1)

5. **Enable S3 access logging** - For audit trail (see Production Hardening in README)
6. **Enable S3 versioning** - For data integrity
7. **Use ACM certificate** - Replace self-signed cert on jumphost
8. **Enable VPC Flow Logs** - For network monitoring
9. ~~**Enable OpenSearch slow logs**~~ - ✅ Now enabled by default

### Long-term Actions (P2)

9. **Implement least privilege** - Separate roles for OSI, Lambda, and admin access
10. **Add WAF** - Protect jumphost from web attacks
11. **Enable CloudTrail** - For comprehensive audit logging
12. **Implement secrets rotation** - Auto-rotate OpenSearch credentials (see Production Hardening in README for guidance on custom rotation Lambda)

## 7. Compliance Considerations

| Framework | Relevant Controls | Status |
|-----------|------------------|--------|
| AWS Well-Architected | SEC01-SEC11 | Partial |
| SOC 2 | CC6.1, CC6.6, CC6.7 | Gaps identified |
| GDPR | Art. 32 (Security) | Review needed for PII handling |

## 8. Attack Scenarios

### Scenario 1: Credential Compromise
1. Attacker gains CloudFormation read access
2. Retrieves master password from stack outputs
3. Accesses OpenSearch via jumphost
4. Exfiltrates or modifies indexed data

### Scenario 2: Lateral Movement
1. Attacker compromises jumphost via SSH
2. Uses nginx proxy to access OpenSearch
3. Exploits overly permissive access policy
4. Gains full cluster access

### Scenario 3: Data Poisoning
1. Attacker gains S3 write access to source bucket
2. Uploads malicious documents
3. OSI pipeline indexes poisoned data
4. Search results compromised

## 9. Residual Risk Assessment

| Risk Category | Current Level | Target Level | Gap |
|---------------|---------------|--------------|-----|
| Authentication | HIGH | LOW | Significant |
| Authorization | HIGH | MEDIUM | Moderate |
| Data Protection | MEDIUM | LOW | Moderate |
| Logging/Monitoring | LOW-MEDIUM | LOW | Minor (slow logs now enabled) |
| Network Security | MEDIUM | LOW | Moderate |

## 10. Document Information

- **Version:** 1.1
- **Date:** March 30, 2026
- **Author:** Security Review
- **Review Cycle:** Quarterly or after significant changes
