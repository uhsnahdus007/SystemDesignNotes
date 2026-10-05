# Chapter 22: Security for System Designers

## Chapter card

| | |
|---|---|
| **Objective** | Give system designers the security vocabulary, mental models, and concrete design choices they need: threat modeling, authentication (passwords, MFA, sessions, tokens, OAuth/OIDC, service identity), authorization (RBAC/ABAC/ReBAC, object-level checks), cryptography in practice (TLS, AEAD, envelope encryption, key management), secrets, network and infrastructure hardening, application-layer defenses, abuse and rate limiting, privacy and compliance, multi-tenancy, and detection/response. Several mechanisms are built and attacked in the lab |
| **Why this chapter exists** | Security is a property of the whole design, not a feature added later. Most breaches come from a small set of design mistakes: missing authorization checks, leaked secrets, reused credentials, unsafe defaults, trusting internal networks. A senior designer anticipates these in the architecture and can talk about them concretely in an interview |
| **Prerequisites** | Chapters 2 (auth basics), 7 (TLS, gateways), 11 (signed URLs), 18 (rate limiting), 21 (logging, audit) |
| **Study time** | 11–14 hours including labs |
| **Tier** | `[MK]` throughout; cryptographic internals `[GK]` |
| **Mastery criteria** | (1) Perform a STRIDE threat model of a design. (2) Explain password hashing, MFA, sessions, JWTs, OAuth 2.0 + PKCE, OIDC, and workload identity. (3) Design authorization (RBAC/ABAC/ReBAC) with object-level checks and tenant isolation. (4) Explain TLS, AEAD, envelope encryption, KMS, and nonce hazards. (5) Design secrets management and rotation. (6) Apply network segmentation, SSRF defenses, and supply-chain controls. (7) Prevent injection, XSS, CSRF, and API-specific attacks. (8) Design rate limiting and credential-stuffing defenses. (9) Address privacy and compliance requirements. (10) Plan detection and incident response |

---

## 22.1 The Story: Five Incidents at VaultPay

**VaultPay** is a payments startup. In its first two years:

1. **The invoice leak.** An API endpoint `GET /invoices/{id}` returns any invoice for an authenticated user. IDs are sequential. A customer writes a loop and downloads 80,000 invoices belonging to other companies. *(Authorization missing at the object level.)*
2. **The account takeover wave.** Attackers take a list of 5 million email/password pairs from another site's breach and try them against VaultPay's login from 8,000 IP addresses. About 1.9% of the pairs work. 96,000 accounts are drained or abused before anyone notices. *(Credential stuffing; password reuse.)*
3. **The key in the repo.** A developer commits a cloud access key to a public repository “temporarily.” Bots find it in four minutes and spin up crypto miners for a $38,000 weekend. *(Secrets in code.)*
4. **The URL previewer.** A “link preview” feature fetches any URL a user supplies. Someone supplies `http://169.254.169.254/latest/meta-data/iam/security-credentials/`, which returns the server's cloud credentials. *(SSRF to the metadata service.)*
5. **The token in the logs.** Debug logging prints request headers, including bearer tokens, which are shipped to a log index readable by 400 employees and a contractor. A log export leaks. *(Sensitive data in telemetry.)*

### Pause & Predict 22.1
> For each incident, name the underlying design principle that was violated (e.g., complete mediation, least privilege, defense in depth, secure by default) and one architectural control that would have prevented or contained it. Which two incidents could have been contained even if the initial mistake had still been made?

---

## 22.2 The security mindset and principles `[MK]`

### 22.2.1 Goals
The **CIA triad:** **C**onfidentiality (only authorized parties read), **I**ntegrity (only authorized changes; detect tampering), **A**vailability (the system works when needed; DoS resistance). Plus **authenticity** (who is it really), **non-repudiation/accountability** (audit: who did what), **privacy** (appropriate use of personal data).

### 22.2.2 Design principles
| Principle | Meaning | Example |
|---|---|---|
| **Least privilege** | Every identity gets the minimum permissions for the minimum time | Service role can read one bucket prefix, not all S3 |
| **Defense in depth** | Multiple independent layers; one failure isn't fatal | WAF + authz in service + encryption + monitoring |
| **Complete mediation** | Check authorization on **every** access, not just at entry | Ownership check on every `GET /invoices/{id}` |
| **Fail closed / secure by default** | On error or ambiguity, deny; defaults are safe | Missing policy → deny |
| **Zero trust** | No implicit trust based on network location; authenticate and authorize every request, internal or external | mTLS + per-call authz between services |
| **Minimize attack surface** | Fewer endpoints, features, dependencies, open ports, privileges | Remove unused APIs, debug endpoints |
| **Separation of duties** | No single person/system can do critical harm alone | Two-person approval for production data access |
| **Assume breach** | Design so a compromise of one component is contained and detected | Segmentation, short-lived credentials, audit logs |
| **Simplicity and reuse** | Use vetted libraries/protocols; don't invent crypto | Use TLS, OIDC, AES-GCM, KMS |
| **Open design** | Security doesn't rely on secrecy of the design, only of keys | Kerckhoffs's principle |
| **Psychological acceptability** | Secure paths must be the easy ones | Passkeys, SSO, paved roads |

---

## 22.3 Threat modeling `[MK]`

### 22.3.1 Process
1. **What are we building?** Draw a data-flow diagram (actors, processes, data stores, data flows) and mark **trust boundaries** (internet↔edge, edge↔services, service↔database, tenant↔tenant, your cloud↔third parties).
2. **What can go wrong?** Apply a framework per element/flow, e.g. **STRIDE**:
| Threat | Violates | Examples | Typical controls |
|---|---|---|---|
| **S**poofing | Authenticity | Stolen credentials, forged tokens, spoofed service | Strong authN, MFA, mTLS, signed tokens |
| **T**ampering | Integrity | Modified request, altered data at rest/in transit | TLS, signatures/HMAC, AEAD, immutable logs, input validation |
| **R**epudiation | Accountability | “I never made that transfer” | Audit logs, signed receipts, time sync |
| **I**nformation disclosure | Confidentiality | IDOR, verbose errors, leaked logs, SSRF | Authz, encryption, redaction, least privilege |
| **D**enial of service | Availability | Floods, expensive queries, resource exhaustion | Rate limits, quotas, autoscaling, WAF/CDN, timeouts |
| **E**levation of privilege | Authorization | Missing checks, injection, confused deputy | Least privilege, RBAC/ABAC, sandboxing, input handling |
3. **What are we going to do about it?** Mitigate, transfer (insurance, managed service), accept (with sign-off), or eliminate (remove the feature). Prioritize by **likelihood × impact** and by cost of the fix.
4. **Did we do a good job?** Validate (tests, review, red-team), track residual risk, update the model when the design changes.

### 22.3.2 Reference lists
- **OWASP Top 10** (web) and **OWASP API Security Top 10**: for APIs the top risks are **Broken Object-Level Authorization (BOLA/IDOR)**, broken authentication, broken object *property*-level authorization (excessive data exposure, mass assignment), unrestricted resource consumption, broken function-level authorization, unrestricted access to sensitive business flows, **SSRF**, security misconfiguration, improper inventory management (shadow/old APIs), unsafe consumption of third-party APIs `[Public]`.
- **Attack trees** for important assets (“steal funds” → compromise admin → phish → bypass MFA …).
- **MITRE ATT&CK** for adversary techniques in detection engineering.

### 22.3.3 A mini threat model of VaultPay's invoice API
Assets: invoice data, customer funds, API keys. Trust boundaries: internet→gateway→invoice service→database; tenant→tenant. Top threats found in 30 minutes: (I) BOLA on `/invoices/{id}`; (S) credential stuffing against login; (T) tampering with amount in a callback (needs HMAC); (D) expensive search queries; (E) mass assignment of `role` in profile update; (I) invoices in logs; (S) forged webhooks. Each maps to a control you can design in before writing code.

---

## 22.4 Authentication: proving who you are `[MK]`

### 22.4.1 Passwords
Rules: never store passwords or reversible encryptions; store a **salted, slow, memory-hard hash**; compare in constant time.
- **Salt:** random per-user value (16+ bytes) stored with the hash, defeating precomputed (rainbow) tables and making identical passwords hash differently.
- **Pepper:** a secret key (kept in an HSM/KMS, not in the DB) mixed in (HMAC) so a stolen database alone is not enough.
- **Algorithms:** **Argon2id** (memory-hard; preferred), **scrypt**, **bcrypt** (cost ≥ 10–12), or **PBKDF2-HMAC-SHA256** with a high iteration count (OWASP's current guidance is ~600,000 iterations for PBKDF2-SHA256; Argon2id with ≥ 19 MiB memory, t = 2) `[Public: OWASP Password Storage Cheat Sheet; check the current values]`. **Never** plain SHA-256/MD5 (fast hashes are the wrong tool).

**Measured (Lab 1, part A).** Single CPU core of the lab machine:
```text
scheme                              time per hash guesses/s per core
SHA-256 (one round, salted or not)       0.122 us        8,214,901
PBKDF2-HMAC-SHA256, 10,000 iters       3.27 ms            305.6
PBKDF2-HMAC-SHA256, 100,000 iters     28.00 ms             35.7
PBKDF2-HMAC-SHA256, 600,000 iters    163.91 ms              6.1
```
And the time for an attacker with **10,000×** this single core (a GPU farm or cluster) to try *every* candidate, assuming the salt is known (it is, after a database breach):
```text
password space                                        SHA-256        PBKDF2 100k        PBKDF2 600k
6-digit PIN (10^6)                                    12.2 us              2.8 s             16.4 s
top-10M leaked passwords                             121.7 us             28.0 s            2.7 min
8 chars, lowercase+digits (36^8)                       34.3 s          91.4 days          1.5 years
10 random lowercase letters (26^10)                  28.6 min           13 years           73 years
```
**Reading it.** (1) A fast hash (SHA-256) evaluates **8 million guesses per second per core**: the entire list of the 10 million most common leaked passwords falls in **~120 microseconds** with a cluster. (2) Raising PBKDF2 to 600,000 iterations costs the *defender* 164 ms per login (affordable) and the attacker the same per guess: the common-password list now takes 2.7 minutes instead of microseconds, and an 8-character lowercase/digit password takes 1.5 years instead of 34 seconds. (3) **Hashing cannot save weak secrets**: a 6-digit PIN falls in 16 seconds even at 600,000 iterations. Defense in depth: breached-password screening, MFA, rate limits, and long random passphrases. (4) Memory-hard functions (Argon2id, scrypt) additionally resist GPU/ASIC parallelism, which this single-core number cannot show.

### 22.4.2 Multi-factor authentication
- **TOTP** (authenticator apps): shared secret + time; vulnerable to real-time phishing.
- **SMS/voice codes:** weakest MFA (SIM swap, interception); acceptable only as a fallback.
- **Push approval:** fatigue attacks (“MFA bombing”); use number matching.
- **WebAuthn/FIDO2/passkeys:** public-key credentials bound to the origin; **phishing-resistant**; the private key never leaves the authenticator; supports passwordless. The strongest practical option.
- **Recovery flows** are the weak link (support-desk social engineering, email resets): design them with the same rigor.

### 22.4.3 Sessions (browser)
After login the server issues a **session ID** (≥128 bits from a CSPRNG) stored server-side (Redis/DB; Ch 6) or a self-contained signed token. Cookie flags: **`HttpOnly`** (not readable by JS, limiting XSS theft), **`Secure`** (HTTPS only), **`SameSite=Lax/Strict`** (CSRF defense), appropriate `Domain`/`Path`, `__Host-` prefix. Rotate the ID on login and privilege change (prevents **session fixation**); idle and absolute timeouts; server-side invalidation on logout/password change; bind to device signals where sensible.

### 22.4.4 Tokens and JWTs `[MK]`
A **JWT** = `base64url(header).base64url(payload).signature`. Signed with **HMAC** (symmetric, `HS256`: both signer and verifier hold the secret) or **public-key** signatures (`RS256/ES256/EdDSA`: services verify with the public key published at a JWKS endpoint, only the issuer holds the private key).
Verification checklist (each item is a real vulnerability class):
1. **Pin the algorithm** on the verifier (reject `alg: none`; prevent RS/HS key-confusion).
2. Verify the **signature** with a constant-time comparison.
3. Check **`exp`** (and `nbf`), **`iss`** (expected issuer), **`aud`** (this service is the intended audience), and required claims/scopes.
4. Use `kid` to select keys from a trusted set; support rotation.
5. Keep **lifetimes short** (5–15 minutes for access tokens).
6. Never put secrets or sensitive PII in the payload (it's only encoded, not encrypted).

**Measured (Lab 1, part B):**
```text
valid token                             -> ACCEPT: {"sub":"alice","exp":1000900}
after expiry (15 minutes later)         -> REJECT: expired
payload edited to sub=admin, old sig   -> REJECT: bad signature
signed with a different key             -> REJECT: bad signature
header says alg=none, signature removed -> REJECT: unexpected algorithm header (never trust the token to choose its own algorithm)
token still ACCEPTED after the user is deleted/logged out until it expires (no revocation list): true
```
The last line is the design trade-off: a self-contained token is verified without any database call (fast, scalable), but **cannot be revoked before it expires**. Mitigations: very short access-token lifetimes plus a **refresh token** (stored server-side, rotated on each use, revocable); a small revocation/denylist checked for sensitive operations; opaque tokens with introspection for high-risk systems. Storing tokens in browsers: prefer `HttpOnly` cookies over `localStorage` (XSS can read the latter).

### 22.4.5 OAuth 2.0 and OpenID Connect `[MK]`
- **OAuth 2.0** is a *delegated authorization* framework: a user lets an app access their data at a resource server without sharing their password. Roles: **resource owner** (user), **client** (app), **authorization server** (issues tokens), **resource server** (API). A **scope** limits what the token can do.
- **Authorization Code flow + PKCE** (the standard for web, mobile, SPAs): the app redirects the user to the authorization server; the user authenticates and consents; the app receives a one-time **code**, exchanges it (with a **PKCE** code verifier proving it started the flow) for tokens. The implicit flow and password grant are deprecated.
- **Client credentials:** machine-to-machine; the client authenticates itself.
- **Device code flow:** TVs/CLI without a browser.
- **OpenID Connect (OIDC)** adds *authentication* on top of OAuth 2.0: an **ID token** (a JWT about the login event and user) and a userinfo endpoint. “Sign in with Google/Okta/Azure AD” uses OIDC. **SAML** is the older XML equivalent common in enterprise SSO.
- Pitfalls: open redirect URIs (require exact match), missing `state` (CSRF) and `nonce`, using access tokens as proof of login, over-broad scopes, tokens in URLs, long-lived refresh tokens without rotation, client secrets in mobile apps.
- Use an established identity provider (Auth0, Okta, Cognito, Keycloak, Azure AD) rather than building auth; implement authorization yourself.

### 22.4.6 Service-to-service identity `[MK/GK]`
Inside the system, services must authenticate each other (zero trust):
- **mTLS:** both sides present certificates; short-lived certs issued by an internal CA; automated rotation (service mesh, cert-manager).
- **Workload identity:** **SPIFFE/SPIRE** issues cryptographic identities (SVIDs) to workloads based on attestation (node, container, namespace) so services carry identity, not static passwords. Cloud equivalents: IAM roles for service accounts (IRSA), GCP workload identity, managed identities.
- **Signed JWTs** for propagating end-user identity across services (with audience and scope restrictions) rather than blindly forwarding the user's token to everyone (**confused deputy**).
- **API keys** are identifiers + weak shared secrets: scope them, rotate them, hash them at rest, rate limit by key, and never put them in URLs or client-side code.
- **Webhook signatures:** the sender signs the body (HMAC-SHA256 over timestamp + body); the receiver verifies, checks freshness (replay protection), and compares in constant time (the signed-URL pattern of Chapter 11).

### 22.4.7 Credential stuffing and account takeover
Attackers replay breached credentials at scale; success rate equals your users' password-reuse rate (1–3%). Per-account lockout does not help (each account is tried once), per-IP limits are evaded with botnets.

**Measured (Lab 1, part E).** 3,000,000 leaked pairs; 2% valid here; 24-hour attack:
```text
scenario                                                                          attempts possible   attempts made   accounts taken over
no controls                                                                        1,440,000,000       3,000,000          60,165
per-IP limit 10/min, attacker has 100 IPs                                              1,440,000       1,440,000          28,660
per-IP limit 10/min, botnet of 10,000 IPs                                            144,000,000       3,000,000          60,239
per-ACCOUNT lockout only (each account tried once)                               14,400,000,000,000       3,000,000          59,399
botnet + MFA on 60% of accounts                                                      144,000,000       3,000,000          24,204
botnet + breached-password screening (80% of reused pw flagged) + MFA 60%            144,000,000       3,000,000           4,734
```
**Reading it.** (1) A per-IP limit helps only against an attacker with few IPs (100 IPs: attempts capped at 1.44 M, halving the damage); a botnet of 10,000 IPs makes it irrelevant (60,239 accounts). (2) **Per-account lockout does nothing** against stuffing (59,399): every account gets one attempt, and lockout is a denial-of-service lever for attackers. (3) **MFA on 60% of accounts cuts takeovers to 24 k**; adding **breached-password screening** (force a reset when a user's password appears in known breach corpora, checked via k-anonymity APIs) reduces it to **4.7 k**: a 92% reduction from the baseline. (4) The remaining levers: bot detection and device/behavioral risk scoring, challenge on anomalous logins (new device/ASN/geo), IP reputation, credential-hash sharing, passkeys, and monitoring login success ratios by ASN/user-agent.

---

## 22.5 Authorization: deciding what you may do `[MK]`

Authentication says *who*; authorization says *what they may do*. Most real vulnerabilities are authorization bugs.

### 22.5.1 Models
| Model | Decision based on | Pros | Cons |
|---|---|---|---|
| **ACL** | Per-object list of principals | Simple, explicit | Doesn't scale; hard to audit |
| **RBAC** (role-based) | User → roles → permissions | Simple, auditable, familiar | Role explosion; coarse; can't express “own records only” |
| **ABAC** (attribute-based) | Policies over attributes of subject, resource, action, environment | Expressive (department = X and amount < 1000 and during business hours) | Complex to author/test/debug |
| **ReBAC** (relationship-based, Zanzibar-style) | Graph of relationships (viewer of doc ← member of group ← org) | Natural for sharing/hierarchies; scalable | Needs a dedicated service; consistency (“new enemy” problem) |
| **Policy engines** | OPA/Rego, Cedar, Casbin | Central, testable policy-as-code | Operational integration |
Google's **Zanzibar** (open implementations: SpiceDB, OpenFGA, Ory Keto) handles billions of ACL checks with a consistency token ("zookie") scheme so a user cannot see content after access was revoked `[Public]`.

### 22.5.2 Where to enforce
- **Policy Enforcement Point (PEP)** in every service (and gateway for coarse checks); **Policy Decision Point (PDP)** central or embedded (sidecar OPA). The **gateway authorization is necessary but never sufficient**: each service must enforce its own object-level rules (defense in depth; the gateway can be bypassed by an internal caller).
- **Complete mediation:** every code path that touches data checks ownership/permissions: including background jobs, GraphQL resolvers, batch endpoints, exports, webhooks, search results, and cached responses (cache keys must include the principal/tenant, Ch 8).
- **Deny by default.** New endpoints require explicit permissions.
- **Object-level (BOLA/IDOR)**, **function-level (BFLA: a normal user calling an admin endpoint)**, and **property-level** (a user updating `role`, `price`, `is_admin` through mass assignment) are three distinct checks.
- **Confused deputy:** a privileged service performs an action on a lower-privileged caller's behalf without checking that the *caller* may do it. Always evaluate the end-user's authority, not the service's.
- **Revocation and propagation delay:** cached authorization decisions and long-lived tokens delay the effect of removing access; define the staleness bound and use short TTLs for sensitive data.

### 22.5.3 Measured: object-level authorization (Lab 1, part D)
100,000 orders belong to 10,000 customers; the attacker (customer #1) makes 100,000 guesses at `GET /orders/{id}`:
```text
A: sequential ids, no ownership check                          99,993 other customers' orders readable
B: sequential ids, ownership check on every request            0 readable
C: random 128-bit ids, no ownership check (by guessing)        expected 2.9e-29 orders readable
D: random 128-bit ids, no check, 1% of ids leaked in logs/links 1,030 readable by anyone who holds the id
(attacker owns 7 orders legitimately.)
```
**Reading it.** With sequential IDs and no check (A) **99,993 of the 100,000 guesses returned someone else's order**: this is VaultPay incident 1. The ownership check (B) reduces unauthorized reads to zero. Unguessable IDs (C) defeat *guessing* (probability 10⁻²⁹) but **are not authorization**: as soon as IDs leak through logs, referrer headers, shared links, browser history, analytics, or screenshots (D: 1% leaked → 1,030 orders readable by anyone holding the link), the missing check is exploitable. Use unguessable IDs to reduce exposure (and avoid enumeration), **and** check authorization on every access.

---

## 22.6 Cryptography for system designers `[MK/GK]`

### 22.6.1 Rules of thumb
1. **Never invent cryptography;** use well-reviewed protocols and libraries (TLS, libsodium/NaCl, Tink, cloud KMS).
2. **Use authenticated encryption (AEAD):** AES-256-GCM or ChaCha20-Poly1305. Plain AES-CBC/CTR without authentication allows undetected tampering.
3. **Use a CSPRNG** (`SecureRandom`, `/dev/urandom`) for keys, nonces, session IDs, tokens: never `Math.random`.
4. **Key management is the hard part:** the algorithm is rarely the weakness; key storage, rotation, and access control are.
5. **Hash ≠ encrypt ≠ sign ≠ MAC:** hash (integrity fingerprint, no key), MAC/HMAC (integrity + authenticity, shared key), signature (authenticity, public-key), encryption (confidentiality).
6. Prefer **elliptic-curve** (Ed25519, ECDSA P-256, X25519) over RSA for new designs; RSA-2048+ if required for compatibility. Plan for **crypto agility** (change algorithms) and post-quantum migration (hybrid key exchange).

### 22.6.2 TLS in practice
- **TLS 1.3**: one-round-trip handshake (Ch 1), forward secrecy by default (ephemeral ECDHE), removed legacy ciphers. Disable TLS ≤ 1.1.
- **Certificates and PKI:** chain to a trusted CA; automate issuance/renewal with ACME/Let's Encrypt/cert-manager (certificate expiry is a top self-inflicted outage); short lifetimes; **HSTS** to prevent downgrade; certificate transparency monitoring; OCSP stapling.
- **mTLS** for service identity (22.4.6); terminate at the edge but **re-encrypt** to backends in zero-trust designs (Ch 7).
- TLS protects data in transit between endpoints, not data at the endpoint or your logs.

### 22.6.3 Encryption at rest and envelope encryption
Levels: full-disk/volume (protects stolen disks), database TDE (protects files, not queries), **application-level** field encryption (protects against DB admins and SQL injection dumps; costs searchability).
**Envelope encryption** (standard in cloud KMS):
```text
 KMS/HSM holds a Key-Encryption Key (KEK / master key) — never leaves the KMS
 for each object/record: generate a fresh Data-Encryption Key (DEK) → encrypt data with AES-GCM(DEK)
                          wrap the DEK with the KEK (KMS call) → store [wrapped DEK ‖ ciphertext]
 read: ask KMS to unwrap the DEK (authorization + audit log) → decrypt locally
```
Benefits: the bulk data never goes through KMS; the master key is never exposed; rotation re-wraps small DEKs rather than re-encrypting terabytes; per-tenant/per-record DEKs limit blast radius and enable **crypto-shredding** (delete the key to erase data, Ch 17); every unwrap is auditable.

**Measured (Lab 1, part C):**
```text
round trip ok: true
flip one ciphertext bit            -> rejected (authentication tag mismatch)
ciphertext replayed under another tenant/record context (AAD mismatch) -> rejected
envelope encryption: 32-byte data key wrapped by the master key into 48 bytes (store wrapped key beside the data; master key never leaves the KMS/HSM)
NONCE REUSE: ciphertext1 XOR ciphertext2 == plaintext1 XOR plaintext2 for all 28 bytes? true  (the keystream cancels; knowing one plaintext reveals the other)
attacker who knows plaintext1 recovers plaintext2 = "TRANSFER $9999900 TO ACC 666"
```
**Reading it.** AES-GCM detects any modification (one flipped bit is rejected) and its **associated data (AAD)** binds a ciphertext to its context: a valid ciphertext copied into another tenant's record fails to decrypt. The last two lines are the classic disaster: **encrypting two messages with the same key and the same nonce** makes the keystreams identical, so `C1 ⊕ C2 = P1 ⊕ P2`; an attacker who knows or guesses one plaintext (a template, a header) recovers the other, and GCM nonce reuse also leaks the authentication key, enabling forgeries. **Nonce rules:** never reuse a (key, nonce) pair; with random 96-bit nonces the birthday bound limits you to about 2³² messages per key (collision probability ≈ n²/2⁹⁷ ≈ 2⁻³³ at n = 2³²); use counters or key-derivation per message (or nonce-misuse-resistant AEAD such as AES-GCM-SIV) and **rotate keys** before the limit.

### 22.6.4 Key management
Hierarchy (root → KEK → DEK), HSM/KMS-protected roots, separation of duties, **key rotation** (automatic for KEKs; DEKs by re-wrapping or periodic re-encryption), key usage policies and audit logs, regional keys for residency, BYOK/HYOK for customers, backup/escrow of keys (losing the key = losing the data), and a tested **revocation/compromise procedure**.

---

## 22.7 Secrets management `[MK]`

**Secrets** = API keys, database passwords, signing keys, tokens, certificates.
- **Never** in source control, container images, build logs, client-side code, tickets, chat, or telemetry. Scan commits and images (git-secrets, truffleHog, GitHub secret scanning) and block pushes.
- **Prefer no static secrets at all:** workload identity (IAM roles, SPIFFE), short-lived credentials issued on demand (Vault dynamic database credentials valid for 1 hour, cloud STS tokens).
- **Secret managers** (HashiCorp Vault, AWS Secrets Manager, GCP Secret Manager, Azure Key Vault): central storage, encryption, access policies, audit, rotation. Applications fetch at startup or via sidecar/CSI driver; cache with TTL; handle rotation without restarts.
- **Least privilege** per secret; separate dev/staging/prod; separate CI secrets with OIDC federation instead of long-lived cloud keys.
- **Rotation:** automated; support two valid versions during rollover; rotate immediately after suspected exposure. **Leak runbook:** revoke first, then investigate (VaultPay incident 3: keys were abused for hours before revocation).
- **Break-glass** emergency access: audited, time-limited, alerting.
- Environment variables are convenient but leak into process listings, crash dumps, and child processes; files/tmpfs or secret-manager APIs are safer.

---

## 22.8 Network, infrastructure, and supply-chain security `[MK/GK]`

- **Segmentation:** VPCs/subnets (public edge, private app tier, isolated data tier), security groups and network policies as default-deny allowlists; separate prod/non-prod accounts; restrict east-west traffic by identity (mesh/NetworkPolicy), not just IP.
- **Admin access:** no public SSH/RDP; use identity-aware proxies / SSM / bastions with MFA and session logging; just-in-time access.
- **Private connectivity:** VPC endpoints/PrivateLink to cloud services and databases (keeps traffic off the internet and cuts NAT costs, Ch 23).
- **Edge protection:** CDN + WAF (OWASP rules, virtual patching), DDoS protection (volumetric L3/L4 absorbed by anycast scrubbing; L7 attacks by rate limits and bot management), origin locked to the CDN (Ch 11).
- **SSRF defenses (incident 4):** never fetch arbitrary user-supplied URLs from a privileged network position; resolve the hostname and **block private/link-local/metadata ranges** (127.0.0.0/8, 10/8, 172.16/12, 192.168/16, 169.254.169.254, IPv6 equivalents) *after* DNS resolution and on every redirect (DNS rebinding); use an allowlist; run fetchers in an isolated, egress-restricted sandbox; require **IMDSv2** (session-token metadata service) and set the hop limit to 1 so containers can't reach it; give workloads least-privilege roles so stolen credentials are low-value.
- **Egress control:** default-deny outbound with allowlists to limit data exfiltration and C2 callbacks.
- **Containers and hosts:** minimal base images, non-root users, read-only root filesystem, dropped capabilities, seccomp/AppArmor, no privileged containers, resource limits, image scanning, runtime detection (Falco), regular patching; sandbox untrusted code with gVisor/Firecracker microVMs.
- **Cloud posture:** infrastructure as code with policy checks (OPA, Checkov, tfsec), CSPM for misconfigurations (public buckets, open security groups, overly broad IAM), account-level guardrails (SCPs).
- **Supply chain:** pin and verify dependencies; SCA scanning; SBOMs; signed commits/artifacts and provenance (SLSA, Sigstore/cosign); private registries; reproducible builds; protect CI runners and secrets; review third-party scripts (Magecart-style browser skimmers); watch for typosquatting and dependency confusion.

---

## 22.9 Application-layer security `[MK]`

| Vulnerability | Mechanism | Defense |
|---|---|---|
| **SQL/NoSQL/command injection** | Untrusted input interpreted as code | **Parameterized queries**/prepared statements, ORMs used correctly, no string concatenation, least-privilege DB user, input validation |
| **XSS** | Untrusted data rendered as script in a victim's browser | Contextual output encoding, template auto-escaping, **Content-Security-Policy**, `HttpOnly` cookies, sanitize rich text with allowlists |
| **CSRF** | Browser automatically attaches cookies to cross-site requests | `SameSite` cookies, CSRF tokens, check `Origin`/`Referer`, don't use GET for state changes |
| **SSRF** | Server fetches attacker-chosen URL | See 22.8 |
| **Insecure deserialization** | Untrusted serialized objects instantiate gadgets | Don't deserialize untrusted native objects; use data-only formats (JSON) with schema validation |
| **Path traversal / file upload** | `../../etc/passwd`, executable uploads | Canonicalize/allowlist, server-chosen names, store outside web root, scan, serve from a separate domain (Ch 11) |
| **XXE** | XML external entities | Disable external entities/DTDs |
| **Mass assignment** | Binding request JSON directly onto model fields (`role`, `isAdmin`) | DTOs with explicit allowlisted fields |
| **Excessive data exposure** | Returning full objects and relying on the UI to hide fields | Response DTOs; field-level authorization |
| **Open redirect** | Redirect to attacker URL after login | Allowlist redirect targets |
| **CORS misconfiguration** | `Access-Control-Allow-Origin: *` with credentials/reflecting origins | Explicit origin allowlist; never reflect arbitrary origins with credentials |
| **Business-logic abuse** | Coupon stacking, race conditions (double spend), enumeration, scalping | Server-side invariants, idempotency, locks/transactions (Ch 3), rate limits per business action, anomaly detection |
| **GraphQL specifics** | Deep/expensive queries, introspection, batching to bypass limits | Depth/complexity limits, persisted queries, per-field authz |
| **Clickjacking** | Page framed by attacker | `X-Frame-Options`/`frame-ancestors` |
| **Error/stack-trace leakage** | Verbose errors reveal internals | Generic client errors; detailed server logs |
Security headers: `Strict-Transport-Security`, `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Permissions-Policy`. Validate input **at trust boundaries** with allowlists and schemas; encode output **for its context**; treat all data from clients, third parties, and even other internal services as untrusted.

---

## 22.10 Abuse, rate limiting, and denial of service `[MK]`

- **Layers of rate limiting:** edge/CDN (per IP, per ASN, geo), gateway (per API key/tenant/user), service (per resource, per operation cost), and **business-action limits** (password reset requests per account, SMS sends per number, coupon redemptions, signups per device). Algorithms: token bucket, sliding window (Ch 18). Return `429` with `Retry-After`.
- **Cost-based limiting:** weight requests by cost (GraphQL query complexity, export size) rather than counting requests.
- **Fairness:** per-tenant quotas and concurrency limits so one customer cannot starve others; priority tiers.
- **L7 DoS:** slowloris (buffering at proxies, timeouts), expensive endpoints (search with wildcards, unbounded pagination: cap page sizes), large payloads (body limits), zip bombs, regex DoS (ReDoS: linear-time engines/timeouts), hash-collision DoS (randomized hashing).
- **Amplification/reflection:** do not expose open resolvers, unauthenticated UDP services, or verbose error amplification; require authentication before expensive work.
- **Scraping and fraud:** bot detection (device fingerprinting, behavioral signals, proof-of-work/CAPTCHA as a last resort), velocity checks, risk scoring, human review queues.
- **Availability under attack:** autoscaling has cost and dependency limits (Ch 6), so rely on CDN/anycast absorption, load shedding (Ch 18), and graceful degradation.

---

## 22.11 Data protection, privacy, and compliance `[MK]`

- **Classify data** (public, internal, confidential, regulated: PII, PHI, PCI) and tie controls to class. **Minimize** collection, **limit retention**, and delete on schedule.
- **Pseudonymization/tokenization:** replace identifiers with tokens (vault maps back) so most systems never touch raw PII/PANs; **PCI DSS scope reduction** by using hosted payment fields/payment providers so card numbers never touch your servers.
- **GDPR/CCPA concepts:** lawful basis and consent, purpose limitation, data subject rights (access, rectification, **erasure**, portability), breach notification within 72 hours (GDPR), data protection by design, data residency/transfer rules (Ch 9, 10: partition by region). **Erasure** across backups, logs, caches, search indexes, analytics, and event logs requires design: crypto-shredding with per-subject keys, short retention for logs, tombstone propagation (Ch 17).
- **HIPAA** (health), **SOX** (financial reporting), **SOC 2 / ISO 27001** (audited control frameworks): compliance ≠ security, but gives structure; automate evidence collection.
- **Audit logging:** who did what to which object when from where, with outcome; write-once/tamper-evident (hash chains/Merkle trees, Ch 20; object lock, Ch 11); protected from the actors it audits; retained per policy; **never log secrets or full PII** (Story incident 5; Ch 21 redaction at the collector).
- **Privacy-enhancing techniques:** aggregation, k-anonymity, differential privacy for analytics, secure enclaves/confidential computing.

---

## 22.12 Multi-tenant security `[MK/GK]`

| Isolation level | Mechanism | Strength | Cost |
|---|---|---|---|
| **Row-level (shared schema)** | `tenant_id` column + **row-level security (RLS)** policies in the database; ORM-enforced scopes | Weakest; one missing `WHERE tenant_id` leaks everything | Cheapest |
| **Schema per tenant** | Separate schema, per-tenant DB role | Better | Medium |
| **Database per tenant** | Separate DB instance | Strong, per-tenant backup/restore | Higher ops |
| **Account/cluster per tenant (silo)** | Separate cloud account/stack | Strongest, regulatory isolation | Highest |
Controls: derive the tenant from the **authenticated identity**, never from a client-supplied parameter; enforce at the **data layer** (RLS) so application bugs can't bypass it; include tenant in **cache keys**, queue topics, object-storage prefixes, and log fields; per-tenant encryption keys (BYOK/crypto-shredding); per-tenant quotas (noisy neighbors, Ch 10, 18); test cross-tenant access explicitly; shuffle-sharding to limit blast radius; sandbox tenant-supplied code. A VaultPay-style invoice leak across companies is a cross-tenant authorization failure.

---

## 22.13 Detection, response, and secure development `[MK]`

- **Security telemetry:** authN failures and anomalies (impossible travel, new device/ASN), privilege changes, access to sensitive objects, data export volumes, API abuse patterns, cloud control-plane events (CloudTrail), WAF blocks, unusual egress; centralize in a **SIEM**, add detection rules (MITRE ATT&CK mapping), canary tokens/honeypots, and alert on integrity changes (file integrity monitoring).
- **Incident response:** preparation (runbooks, contacts, forensic tooling), detection, **containment** (revoke tokens/keys, isolate hosts, block IPs), eradication (patch, rotate), recovery, **post-incident review**, notification (customers, regulators, law enforcement). Practice with tabletop exercises and red-team/purple-team drills.
- **Vulnerability management:** SAST/DAST/SCA in CI, dependency updates with SLAs by severity, penetration tests, bug-bounty and a published disclosure policy (`security.txt`), patching cadence.
- **Secure SDLC:** threat modeling at design time, security requirements in tickets, code review checklists (authz, input handling, secrets), secure defaults in frameworks/templates, fuzzing for parsers, security champions in teams.
- **Metrics:** mean time to detect/contain, % services with authz tests, secrets-scan findings, patch latency, MFA coverage.

---

## 22.14 Failure modes and anti-patterns

| Anti-pattern | Consequence | Fix |
|---|---|---|
| **Authorization only at the gateway/UI** | IDOR/BOLA through direct service calls | Enforce per object in every service |
| **Sequential IDs without checks** | Mass enumeration (99,993 readable in lab) | Ownership checks; opaque IDs as defense in depth |
| **Fast hashes for passwords** | 8 M guesses/s/core | Argon2id/bcrypt/scrypt/PBKDF2 with high cost |
| **No MFA / SMS-only MFA** | Account takeover | WebAuthn/passkeys, TOTP |
| **Per-account lockout as only defense** | No effect on stuffing; DoS lever | Breach screening, MFA, risk-based challenges |
| **Long-lived bearer tokens, no revocation path** | Stolen token valid for days | Short TTL + rotating refresh tokens |
| **Trusting JWT headers / not validating aud/iss** | alg confusion, token reuse across services | Pin algorithms; validate all claims |
| **Secrets in code/images/logs** | Instant compromise | Secret manager, workload identity, scanning |
| **Static, shared, over-privileged credentials** | Large blast radius | Short-lived, least privilege, per-workload |
| **Internal network trusted** | Lateral movement after one breach | Zero trust, mTLS, segmentation |
| **Fetching user URLs from privileged networks** | SSRF → cloud credentials | Egress filtering, allowlists, IMDSv2 |
| **Nonce/IV reuse** | Plaintext recovery, forgery | Unique nonces, rotate keys, AEAD |
| **Home-grown crypto** | Subtle breaks | Vetted libraries |
| **Encryption without key management** | Keys beside data | KMS/HSM, envelope encryption, rotation |
| **Verbose errors / debug endpoints in prod** | Information leaks | Generic errors, remove debug |
| **Logging sensitive data** | Secondary breach vector | Redaction, allowlist logging |
| **Unbounded APIs** | DoS, scraping | Quotas, pagination caps, cost-based limits |
| **Mass assignment** | Privilege escalation | DTO allowlists |
| **Shared-schema tenancy without RLS** | One bug leaks all tenants | RLS, tenant from identity |
| **Security as a late-stage review** | Expensive redesign | Threat model at design time |
| **Compliance theater** | Passes audit, still breached | Real controls and testing |
| **No offboarding/rotation** | Ex-employee/contractor access | SSO, deprovisioning, periodic access review |

---

## 22.15 Labs

### Lab 1: Password hashing, tokens, AES-GCM, authorization, and credential stuffing (Java 17+)
```java
import javax.crypto.*;
import javax.crypto.spec.*;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.util.*;

public class SecLab {
    static final SecureRandom SR = new SecureRandom();
    static String hex(byte[] b, int n) { StringBuilder s = new StringBuilder(); for (int i = 0; i < Math.min(n, b.length); i++) s.append(String.format("%02x", b[i])); return s.toString(); }

    // ====================================================================== A) password hashing cost
    static void passwordHashing() throws Exception {
        System.out.println("== A) Password hashing: why 'fast' hashes are the wrong tool. Single CPU core of this machine. ==");
        MessageDigest sha = MessageDigest.getInstance("SHA-256"); byte[] pw = "correct horse".getBytes(StandardCharsets.UTF_8);
        for (int i = 0; i < 200_000; i++) sha.digest(pw);                                  // warm-up
        int n = 2_000_000; long t0 = System.nanoTime(); for (int i = 0; i < n; i++) sha.digest(pw); double shaRate = n / ((System.nanoTime() - t0) / 1e9);
        System.out.printf("%-34s %14s %16s%n", "scheme", "time per hash", "guesses/s per core");
        System.out.printf("%-34s %11.3f us %,16.0f%n", "SHA-256 (one round, salted or not)", 1e6 / shaRate, shaRate);
        SecretKeyFactory f = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256"); byte[] salt = new byte[16]; SR.nextBytes(salt); double[] rates = new double[3]; int[] its = {10_000, 100_000, 600_000};
        for (int w = 0; w < 30; w++) f.generateSecret(new PBEKeySpec("warm".toCharArray(), salt, 100_000, 256));              // JIT warm-up so the first row is not penalized
        for (int k = 0; k < its.length; k++) {
            char[] c = "correct horse".toCharArray(); f.generateSecret(new PBEKeySpec(c, salt, its[k], 256));              // warm-up
            int reps = Math.max(2, 600_000 / its[k] * 3); t0 = System.nanoTime(); for (int i = 0; i < reps; i++) f.generateSecret(new PBEKeySpec(c, salt, its[k], 256)); double sec = (System.nanoTime() - t0) / 1e9 / reps;
            rates[k] = 1 / sec; System.out.printf("%-34s %8.2f ms %,16.1f%n", "PBKDF2-HMAC-SHA256, " + String.format("%,d", its[k]) + " iters", sec * 1000, rates[k]);
        }
        System.out.println("Time for an attacker with 10,000x this single core (a GPU farm / cluster) to try EVERY candidate, assuming the attacker knows the salt:");
        System.out.printf("%-44s %16s %18s %18s%n", "password space", "SHA-256", "PBKDF2 100k", "PBKDF2 600k");
        record Space(String name, double size) {}
        for (Space sp : new Space[]{new Space("6-digit PIN (10^6)", 1e6), new Space("top-10M leaked passwords", 1e7), new Space("8 chars, lowercase+digits (36^8)", Math.pow(36, 8)), new Space("10 random lowercase letters (26^10)", Math.pow(26, 10))}) {
            System.out.printf("%-44s %16s %18s %18s%n", sp.name(), human(sp.size() / (shaRate * 1e4)), human(sp.size() / (rates[1] * 1e4)), human(sp.size() / (rates[2] * 1e4)));
        }
    }
    static String human(double s) { if (s < 1e-3) return String.format("%.1f us", s * 1e6); if (s < 1) return String.format("%.1f ms", s * 1e3); if (s < 60) return String.format("%.1f s", s); if (s < 3600) return String.format("%.1f min", s / 60); if (s < 86400) return String.format("%.1f h", s / 3600); if (s < 86400 * 365) return String.format("%.1f days", s / 86400); double y = s / 86400 / 365; return y < 10 ? String.format("%.1f years", y) : String.format("%,.0f years", y); }

    // ====================================================================== B) signed tokens
    static byte[] hmac(byte[] key, String data) throws Exception { Mac m = Mac.getInstance("HmacSHA256"); m.init(new SecretKeySpec(key, "HmacSHA256")); return m.doFinal(data.getBytes(StandardCharsets.UTF_8)); }
    static String b64(byte[] b) { return Base64.getUrlEncoder().withoutPadding().encodeToString(b); }
    static String issue(byte[] key, String sub, long exp) throws Exception { String payload = b64(("{\"sub\":\"" + sub + "\",\"exp\":" + exp + "}").getBytes(StandardCharsets.UTF_8)); String header = b64("{\"alg\":\"HS256\"}".getBytes()); String body = header + "." + payload; return body + "." + b64(hmac(key, body)); }
    static String verify(byte[] key, String token, long now) throws Exception {
        String[] p = token.split("\\.", -1); if (p.length != 3) return "REJECT: malformed";
        String header = new String(Base64.getUrlDecoder().decode(p[0]), StandardCharsets.UTF_8); if (!header.equals("{\"alg\":\"HS256\"}")) return "REJECT: unexpected algorithm header (never trust the token to choose its own algorithm)";
        byte[] expected = hmac(key, p[0] + "." + p[1]); byte[] got = Base64.getUrlDecoder().decode(p[2]);
        if (!MessageDigest.isEqual(expected, got)) return "REJECT: bad signature";                          // constant-time comparison
        String payload = new String(Base64.getUrlDecoder().decode(p[1]), StandardCharsets.UTF_8); long exp = Long.parseLong(payload.replaceAll(".*\"exp\":(\\d+).*", "$1"));
        if (now > exp) return "REJECT: expired"; return "ACCEPT: " + payload;
    }
    static void tokens() throws Exception {
        System.out.println("\n== B) Signed (HMAC) tokens: what verification must check ==");
        byte[] key = new byte[32]; SR.nextBytes(key); long now = 1_000_000; String tok = issue(key, "alice", now + 900); String[] p = tok.split("\\.");
        System.out.println("valid token                             -> " + verify(key, tok, now + 10));
        System.out.println("after expiry (15 minutes later)         -> " + verify(key, tok, now + 901));
        String forged = p[0] + "." + b64(("{\"sub\":\"admin\",\"exp\":" + (now + 900) + "}").getBytes()) + "." + p[2];
        System.out.println("payload edited to sub=admin, old sig   -> " + verify(key, forged, now + 10));
        byte[] otherKey = new byte[32]; SR.nextBytes(otherKey);
        System.out.println("signed with a different key             -> " + verify(key, issue(otherKey, "alice", now + 900), now + 10));
        String noneHeader = b64("{\"alg\":\"none\"}".getBytes()); System.out.println("header says alg=none, signature removed -> " + verify(key, noneHeader + "." + p[1] + ".", now + 10));
        System.out.println("token still ACCEPTED after the user is deleted/logged out until it expires (no revocation list): " + verify(key, tok, now + 20).startsWith("ACCEPT"));
    }

    // ====================================================================== C) authenticated encryption and nonce reuse
    static void aesGcm() throws Exception {
        System.out.println("\n== C) AES-GCM: confidentiality + integrity, and the catastrophic nonce-reuse failure ==");
        KeyGenerator kg = KeyGenerator.getInstance("AES"); kg.init(256); SecretKey kek = kg.generateKey(), dek = kg.generateKey();
        byte[] nonce = new byte[12]; SR.nextBytes(nonce); byte[] aad = "tenant=acme;record=42".getBytes();
        Cipher enc = Cipher.getInstance("AES/GCM/NoPadding"); enc.init(Cipher.ENCRYPT_MODE, dek, new GCMParameterSpec(128, nonce)); enc.updateAAD(aad);
        byte[] pt = "card=4111111111111111;amount=120.00".getBytes(); byte[] ct = enc.doFinal(pt);
        Cipher dec = Cipher.getInstance("AES/GCM/NoPadding"); dec.init(Cipher.DECRYPT_MODE, dek, new GCMParameterSpec(128, nonce)); dec.updateAAD(aad);
        System.out.println("round trip ok: " + new String(dec.doFinal(ct)).equals(new String(pt)));
        byte[] tampered = ct.clone(); tampered[3] ^= 1; try { Cipher d2 = Cipher.getInstance("AES/GCM/NoPadding"); d2.init(Cipher.DECRYPT_MODE, dek, new GCMParameterSpec(128, nonce)); d2.updateAAD(aad); d2.doFinal(tampered); System.out.println("tampered ciphertext accepted (BUG)"); } catch (AEADBadTagException ex) { System.out.println("flip one ciphertext bit            -> rejected (authentication tag mismatch)"); }
        try { Cipher d3 = Cipher.getInstance("AES/GCM/NoPadding"); d3.init(Cipher.DECRYPT_MODE, dek, new GCMParameterSpec(128, nonce)); d3.updateAAD("tenant=evil;record=42".getBytes()); d3.doFinal(ct); System.out.println("wrong associated data accepted (BUG)"); } catch (AEADBadTagException ex) { System.out.println("ciphertext replayed under another tenant/record context (AAD mismatch) -> rejected"); }
        // envelope encryption: the data key is wrapped by the key-encryption key
        byte[] wnonce = new byte[12]; SR.nextBytes(wnonce); Cipher wrap = Cipher.getInstance("AES/GCM/NoPadding"); wrap.init(Cipher.ENCRYPT_MODE, kek, new GCMParameterSpec(128, wnonce)); byte[] wrapped = wrap.doFinal(dek.getEncoded());
        System.out.println("envelope encryption: 32-byte data key wrapped by the master key into " + wrapped.length + " bytes (store wrapped key beside the data; master key never leaves the KMS/HSM)");
        // nonce reuse
        byte[] p1 = "TRANSFER $0000100 TO ACC 111".getBytes(), p2 = "TRANSFER $9999900 TO ACC 666".getBytes();
        Cipher e1 = Cipher.getInstance("AES/GCM/NoPadding"); e1.init(Cipher.ENCRYPT_MODE, dek, new GCMParameterSpec(128, nonce)); byte[] c1 = e1.doFinal(p1);
        Cipher e2 = Cipher.getInstance("AES/GCM/NoPadding"); e2.init(Cipher.ENCRYPT_MODE, dek, new GCMParameterSpec(128, nonce)); byte[] c2 = e2.doFinal(p2);     // SAME key and SAME nonce: forbidden
        boolean leak = true; for (int i = 0; i < p1.length; i++) if ((c1[i] ^ c2[i]) != (p1[i] ^ p2[i])) leak = false;
        System.out.println("NONCE REUSE: ciphertext1 XOR ciphertext2 == plaintext1 XOR plaintext2 for all " + p1.length + " bytes? " + leak + "  (the keystream cancels; knowing one plaintext reveals the other)");
        byte[] known = new byte[p1.length]; for (int i = 0; i < p1.length; i++) known[i] = (byte) (c1[i] ^ c2[i] ^ p1[i]);
        System.out.println("attacker who knows plaintext1 recovers plaintext2 = \"" + new String(known) + "\"");
    }

    // ====================================================================== D) object-level authorization (IDOR / BOLA)
    static void idor() {
        System.out.println("\n== D) Object-level authorization. 100,000 orders belong to 10,000 customers. An attacker (customer #1) calls GET /orders/{id} for 100,000 guesses. ==");
        int orders = 100_000, customers = 10_000; int[] owner = new int[orders]; Random r = new Random(7); for (int i = 0; i < orders; i++) owner[i] = r.nextInt(customers);
        long mine = 0; for (int o : owner) if (o == 1) mine++;
        long leakedA = 0, leakedB = 0, leakedC = 0, leakedD = 0;
        for (int id = 0; id < orders; id++) { leakedA += owner[id] == 1 ? 0 : 1; }                                    // A: sequential ids, no ownership check -> every guess hits someone else's order
        // B: sequential ids WITH an ownership check -> 0
        // C: random 128-bit ids, no ownership check: guessing is hopeless
        double pGuess = 100_000.0 * orders / Math.pow(2, 128);
        // D: random ids, no check, but 1% of ids leak via logs, referrer headers, shared links, analytics
        for (int id = 0; id < orders; id++) if (r.nextDouble() < 0.01 && owner[id] != 1) leakedD++;
        System.out.printf("%-62s %s%n", "A: sequential ids, no ownership check", String.format("%,d other customers' orders readable", leakedA));
        System.out.printf("%-62s %s%n", "B: sequential ids, ownership check on every request", "0 readable");
        System.out.printf("%-62s %s%n", "C: random 128-bit ids, no ownership check (by guessing)", String.format("expected %.1e orders readable", pGuess));
        System.out.printf("%-62s %s%n", "D: random 128-bit ids, no check, 1% of ids leaked in logs/links", String.format("%,d readable by anyone who holds the id", leakedD));
        System.out.println("(attacker owns " + mine + " orders legitimately.) Unguessable ids shrink the attack surface but are NOT an authorization control; the check must run on every object access.");
    }

    // ====================================================================== E) credential stuffing vs defenses
    static void credentialStuffing() {
        System.out.println("\n== E) Credential stuffing. Attacker holds 3,000,000 leaked (username, password) pairs from another site; 2% of the pairs are valid here (password reuse). 24-hour attack. ==");
        long pairs = 3_000_000; double reuse = 0.02; Random r = new Random(11);
        record Sc(String name, long ips, int perIpPerMin, double mfaCoverage, double breachScreen) {}
        Sc[] scs = {new Sc("no controls", 1, 1_000_000, 0, 0), new Sc("per-IP limit 10/min, attacker has 100 IPs", 100, 10, 0, 0), new Sc("per-IP limit 10/min, botnet of 10,000 IPs", 10_000, 10, 0, 0),
                    new Sc("per-ACCOUNT lockout only (each account tried once)", 10_000, 1_000_000, 0, 0), new Sc("botnet + MFA on 60% of accounts", 10_000, 10, 0.6, 0), new Sc("botnet + breached-password screening (80% of reused pw flagged) + MFA 60%", 10_000, 10, 0.6, 0.8)};
        System.out.println("scenario                                                                          attempts possible   attempts made   accounts taken over");
        for (Sc s : scs) {
            long capacity = s.ips() * s.perIpPerMin() * 1440L; long attempts = Math.min(pairs, capacity); long taken = 0;
            for (long i = 0; i < attempts; i++) { if (r.nextDouble() < reuse) { if (r.nextDouble() < s.breachScreen()) continue; if (r.nextDouble() < s.mfaCoverage()) continue; taken++; } }
            System.out.printf("%-80s %,15d %,15d %,15d%n", s.name(), capacity, attempts, taken);
        }
    }

    public static void main(String[] a) throws Exception { passwordHashing(); tokens(); aesGcm(); idor(); credentialStuffing(); }
}
```
**Sample output** (part A timings depend on the machine; the ratios are the lesson)
```text
== A) Password hashing: why 'fast' hashes are the wrong tool. Single CPU core of this machine. ==
scheme                              time per hash guesses/s per core
SHA-256 (one round, salted or not)       0.122 us        8,214,901
PBKDF2-HMAC-SHA256, 10,000 iters       3.27 ms            305.6
PBKDF2-HMAC-SHA256, 100,000 iters     28.00 ms             35.7
PBKDF2-HMAC-SHA256, 600,000 iters    163.91 ms              6.1
Time for an attacker with 10,000x this single core (a GPU farm / cluster) to try EVERY candidate, assuming the attacker knows the salt:
password space                                        SHA-256        PBKDF2 100k        PBKDF2 600k
6-digit PIN (10^6)                                    12.2 us              2.8 s             16.4 s
top-10M leaked passwords                             121.7 us             28.0 s            2.7 min
8 chars, lowercase+digits (36^8)                       34.3 s          91.4 days          1.5 years
10 random lowercase letters (26^10)                  28.6 min           13 years           73 years

== B) Signed (HMAC) tokens: what verification must check ==
valid token                             -> ACCEPT: {"sub":"alice","exp":1000900}
after expiry (15 minutes later)         -> REJECT: expired
payload edited to sub=admin, old sig   -> REJECT: bad signature
signed with a different key             -> REJECT: bad signature
header says alg=none, signature removed -> REJECT: unexpected algorithm header (never trust the token to choose its own algorithm)
token still ACCEPTED after the user is deleted/logged out until it expires (no revocation list): true

== C) AES-GCM: confidentiality + integrity, and the catastrophic nonce-reuse failure ==
round trip ok: true
flip one ciphertext bit            -> rejected (authentication tag mismatch)
ciphertext replayed under another tenant/record context (AAD mismatch) -> rejected
envelope encryption: 32-byte data key wrapped by the master key into 48 bytes (store wrapped key beside the data; master key never leaves the KMS/HSM)
NONCE REUSE: ciphertext1 XOR ciphertext2 == plaintext1 XOR plaintext2 for all 28 bytes? true  (the keystream cancels; knowing one plaintext reveals the other)
attacker who knows plaintext1 recovers plaintext2 = "TRANSFER $9999900 TO ACC 666"

== D) Object-level authorization. 100,000 orders belong to 10,000 customers. An attacker (customer #1) calls GET /orders/{id} for 100,000 guesses. ==
A: sequential ids, no ownership check                          99,993 other customers' orders readable
B: sequential ids, ownership check on every request            0 readable
C: random 128-bit ids, no ownership check (by guessing)        expected 2.9e-29 orders readable
D: random 128-bit ids, no check, 1% of ids leaked in logs/links 1,030 readable by anyone who holds the id
(attacker owns 7 orders legitimately.) Unguessable ids shrink the attack surface but are NOT an authorization control; the check must run on every object access.

== E) Credential stuffing. Attacker holds 3,000,000 leaked (username, password) pairs from another site; 2% of the pairs are valid here (password reuse). 24-hour attack. ==
scenario                                                                          attempts possible   attempts made   accounts taken over
no controls                                                                        1,440,000,000       3,000,000          60,165
per-IP limit 10/min, attacker has 100 IPs                                              1,440,000       1,440,000          28,660
per-IP limit 10/min, botnet of 10,000 IPs                                            144,000,000       3,000,000          60,239
per-ACCOUNT lockout only (each account tried once)                               14,400,000,000,000       3,000,000          59,399
botnet + MFA on 60% of accounts                                                      144,000,000       3,000,000          24,204
botnet + breached-password screening (80% of reused pw flagged) + MFA 60%            144,000,000       3,000,000           4,734
```
**Experiments**
1. In part A, add **salted SHA-256 with 100,000 manual iterations** and compare with PBKDF2; then implement **Argon2id** (via a library such as Bouncy Castle) and compare memory and time. Compute the cost for an attacker with GPUs: why does memory-hardness help?
2. In part A, compute the cost-to-defender: at 1,000 logins per second, how many cores does PBKDF2-600k require? Propose how to avoid login CPU saturation (rate limiting, queueing, separate auth service, adaptive cost).
3. In part B, implement an `HS256` → `RS256` algorithm-confusion attack on a verifier that trusts the header (use the public key as the HMAC secret) and show it succeeds; then pin the algorithm and show it fails. Add `aud`/`iss` checks and a `kid`-based key rotation.
4. In part B, add a **refresh-token rotation with reuse detection**: if an old refresh token is presented twice, revoke the whole token family.
5. In part C, implement envelope encryption with a software "KMS" class (wrap/unwrap with audit log), per-tenant DEKs, and **key rotation by re-wrapping**; then crypto-shred one tenant by deleting its KEK and prove the data is unreadable.
6. In part C, calculate with a script the collision probability for random 96-bit nonces at 2²⁰, 2³⁰, 2³² messages per key; implement a counter-based nonce scheme and show that restart-without-persisting the counter reintroduces nonce reuse.
7. In part D, implement an authorization layer (ownership + tenant scoping) as middleware and write tests that attempt BOLA, BFLA, and mass assignment against it. Add a Postgres RLS policy and show it blocks a query missing `WHERE tenant_id`.
8. In part E, add a **risk-based challenge** (new device or ASN triggers MFA) and **device cookies**, and measure the trade-off between takeovers prevented and legitimate users challenged under assumed login-pattern distributions.

### Lab 2: OAuth 2.0 + OIDC with Keycloak, PKCE, and a resource server (needs Docker; not executed in the book's sandbox)
Run Keycloak (`quay.io/keycloak/keycloak start-dev`), create a realm, a public client for a SPA/CLI and a confidential client for a backend. Walk through the **authorization code + PKCE** flow with `curl` and a browser, decode the ID and access tokens, configure a resource server (the Chapter 2 TaskApi behind a small JWT-verifying filter) that validates signature (via JWKS), `iss`, `aud`, `exp`, and scope. Rotate the signing key and observe `kid` handling. Try a stolen access token after it expires, and a refresh token reuse.

### Lab 3: Threat-model a design (paper)
Take the Chapter 11 photo-upload pipeline or the Chapter 12 order-checkout flow. Draw the data-flow diagram with trust boundaries; fill a STRIDE table (at least 12 threats); rank by likelihood × impact; for the top 5 specify the control, where it is enforced, how you would test it, and what telemetry detects an attempt. List what you decided *not* to mitigate and why.

---

## 22.16 Interview perspective

- Begin with **who the adversaries are and what the assets are** (threat model), then walk the trust boundaries.
- Authentication: **SSO via OIDC (authorization code + PKCE), MFA/passkeys, Argon2id/bcrypt, short-lived access tokens + rotating refresh tokens**. Service-to-service: **mTLS/workload identity**.
- Authorization: **deny by default, enforce at every service (not just the gateway), object-level checks, tenant isolation with RLS**; mention RBAC/ABAC/ReBAC trade-offs.
- Data: **TLS 1.3 in transit, envelope encryption with KMS at rest, per-tenant keys, tokenization for PCI/PII**, retention and erasure plans.
- Secrets: **no static secrets, secret manager, rotation, scanning**.
- Abuse: **rate limits at multiple layers, credential-stuffing defenses (breach screening, MFA, bot/risk signals), WAF/CDN**.
- Operations: **audit logs, SIEM detections, incident response, least privilege, patching, supply-chain controls**.
- Show restraint: don't claim "encrypt everything" solves authorization; name the residual risks.

---

## 22.17 Common misconceptions

1. **“We use HTTPS, so we're secure.”** TLS protects transport only.
2. **“Random IDs make IDOR impossible.”** They hide guessing, not authorization (1,030 leaked-ID reads in the lab).
3. **“SHA-256 is a secure password hash.”** It's too fast (8 M/s/core).
4. **“Account lockout stops brute force.”** It doesn't stop credential stuffing and enables DoS.
5. **“JWTs are encrypted.”** They're signed, not encrypted; payloads are readable.
6. **“Stateless tokens can be revoked.”** Not without extra state.
7. **“Internal network = trusted.”** Lateral movement makes this the root of many breaches.
8. **“Encryption at rest means the data is safe from the application.”** The application holds the keys.
9. **“The gateway handles security.”** Services need their own authorization.
10. **“Security is the security team's job.”** Design decisions create the vulnerabilities.
11. **“Compliance equals security.”** Different goals.
12. **“We're too small to be a target.”** Attacks are automated and indiscriminate.

---

## 22.18 Chapter summary

- Security principles: **least privilege, defense in depth, complete mediation, fail closed, zero trust, assume breach**. Use **STRIDE** on a data-flow diagram with trust boundaries; know the **OWASP API Top 10** (BOLA first).
- **Passwords:** salted, slow, memory-hard hashes. Fast SHA-256 allows 8.2 M guesses/s/core (top-10M list in ~120 µs on a cluster) vs PBKDF2-600k at 6 guesses/s/core (2.7 minutes for the same list; 8-char lowercase+digits from 34 s to 1.5 years). Hashing can't save short secrets (6-digit PIN: 16 s).
- **MFA/passkeys** (WebAuthn) are the strongest account defenses; **sessions** need `HttpOnly/Secure/SameSite` cookies and rotation; **JWTs** need algorithm pinning and full claim validation and can't be revoked before expiry (lab); use **OAuth 2.0 authorization code + PKCE** and **OIDC**; service identity via **mTLS/SPIFFE/workload identity**.
- **Credential stuffing:** per-IP limits fail against botnets, per-account lockout does nothing; MFA (60% coverage) + breached-password screening cut takeovers from 60 k to 4.7 k in the lab.
- **Authorization:** RBAC/ABAC/ReBAC, deny by default, enforce object/function/property-level checks in every service; sequential IDs without checks exposed **99,993/100,000** orders; opaque IDs reduce guessing but leaked ones still work (1,030).
- **Crypto:** AEAD (AES-GCM) detects tampering and binds context (AAD); **nonce reuse leaks plaintext** (XOR relation verified) and keys; **envelope encryption** with KMS/HSM, per-tenant keys, rotation, crypto-shredding.
- **Secrets:** none in code; secret managers; short-lived workload credentials; rotation and leak runbooks.
- **Infrastructure:** segmentation, private endpoints, WAF/DDoS, SSRF defenses (block metadata ranges after DNS resolution, IMDSv2), hardened containers, supply-chain controls.
- **Application security:** parameterized queries, output encoding/CSP, CSRF defenses, DTO allowlists, schema validation, secure headers; **abuse controls**: layered, cost-based rate limits.
- **Privacy/compliance:** classification, minimization, retention, tokenization, erasure design, audit logs; **multi-tenancy** needs identity-derived tenant context and data-layer enforcement.
- **Operate securely:** detection (SIEM), incident response, vulnerability management, secure SDLC.

### Mental models
| Concept | Mental model |
|---|---|
| Defense in depth | A castle: moat, walls, gate, guards, locked rooms |
| Least privilege | A hotel key card that opens only your room, only for your stay |
| Zero trust | Show ID at every door, even inside the building |
| Password hashing | A deliberately slow lock-picking test |
| Salt | Each safe gets its own serial number so one pick doesn't open all |
| JWT | A signed concert ticket: valid until it expires, even if you are banned meanwhile |
| OAuth | A valet key: limited access without handing over the master key |
| Envelope encryption | Each document in its own locked folder, folder keys kept in the bank vault |
| Nonce reuse | Using the same one-time pad twice |
| BOLA | The hotel front desk handing out any room's key if you read the number aloud |
| Credential stuffing | Trying the key that opened your neighbor's old apartment on every door in the city |

---

## 22.19 Exercises

### A. Conceptual
1. Define confidentiality, integrity, availability, authenticity, and non-repudiation with a VaultPay example of each.
2. Explain least privilege, defense in depth, complete mediation, fail-closed, and zero trust, each with a design example.
3. Perform a STRIDE analysis of a login endpoint (at least one threat per category and a control).
4. Why must passwords be hashed with a slow, salted function? What do salt and pepper each defend against?
5. Why can't a good hash protect a 6-digit PIN? What else do you need?
6. Compare TOTP, SMS, push, and WebAuthn on phishing resistance and usability.
7. Describe a safe session cookie configuration and session-fixation prevention.
8. List the checks a JWT verifier must perform. What is the `alg: none` attack and the RS/HS confusion attack?
9. Why can't a stateless JWT be revoked? List three mitigations and their costs.
10. Walk through the OAuth 2.0 authorization code + PKCE flow and explain what PKCE prevents.
11. Distinguish OAuth 2.0, OIDC, and SAML.
12. Compare RBAC, ABAC, and ReBAC with an example where each is best.
13. Explain BOLA, BFLA, and mass assignment, and a control for each.
14. Why is gateway-only authorization insufficient? What is a confused deputy?
15. Explain AEAD and AAD. Why is AES-CBC without a MAC dangerous?
16. Describe envelope encryption and three benefits. How does crypto-shredding work?
17. What happens when you reuse an AES-GCM nonce under the same key? How do you prevent it?
18. List the elements of a secrets-management strategy and a leaked-key runbook.
19. Explain SSRF and a layered defense, including metadata service hardening.
20. Why do per-IP limits and per-account lockout fail against credential stuffing? What works?
21. How do you implement tenant isolation in a shared database? Why is RLS better than application-level `WHERE` clauses?
22. What belongs in an audit log and what must never be logged? How do you make it tamper-evident?
23. Outline an incident response plan for a leaked database credential.

### B. Calculations
24. Password entropy: compute bits for (a) 8 random chars from 94 printable ASCII, (b) 4 random words from a 7,776-word list, (c) 10 random lowercase letters, (d) a 6-digit PIN. At 10¹¹ guesses/s (fast hash with GPUs), how long to exhaust each? At 10⁴ guesses/s (slow hash)?
25. Using the lab's single-core numbers (PBKDF2-600k: 6.1 hashes/s per core), how many cores to sustain 500 logins/s? Compare with PBKDF2-100k (35.7/s) and 10k (305.6/s). What are the security trade-offs?
26. AES-GCM random 96-bit nonces: compute the probability of at least one collision after 2²⁰, 2³⁰, 2³², and 2⁴⁰ messages under one key (use n²/2⁹⁷). What rotation policy follows?
27. Credential stuffing: 8 M leaked pairs, 1.5% reuse, botnet of 5,000 IPs with a per-IP limit of 20 attempts/min, attack lasts 12 hours. Attempts possible vs available; accounts taken over without MFA; with MFA at 70%; with breach screening of 85%.
28. Token lifetimes: access token 10 minutes, refresh token 14 days, rotation on every use. A refresh token leaks and is used by the attacker 2 days later; the legitimate user then refreshes. What detection is possible and what is the window of exposure?
29. RBAC role explosion: 40 permissions, users need arbitrary combinations from 6 teams × 5 environments × 3 access levels. Number of distinct roles needed for all combinations if you create one role per combination, vs ABAC rule count?
30. Rate limiting design: token bucket of 20 requests burst, 5/s refill. A client sends 100 requests in the first second then 5/s steadily. How many are accepted in the first 4 seconds? Design a cost-based limit for an export endpoint costing 50 tokens.
31. A webhook endpoint accepts HMAC-signed requests with a 5-minute timestamp tolerance. An attacker captures a valid request. How long can it be replayed? How would you cut replay to zero using a nonce store; how big is it for 10,000 webhooks/minute?
32. Encryption overhead: AES-GCM at 3 GB/s per core; the service encrypts 2 GB/s of traffic at rest. Cores needed? KMS unwrap calls: 20,000 requests/s each needing a DEK unwrap vs DEK caching with a 5-minute TTL and 2,000 distinct DEKs. Compute the KMS call rates.

### C. Design
33. Design authentication and authorization for a multi-tenant SaaS with web, mobile, and public API clients: identity provider, flows, token formats and lifetimes, tenant model, service-to-service auth, and revocation.
34. Design the secrets and key management for a platform with 200 services: workload identity, secret manager, KMS hierarchy, rotation, and break-glass.
35. Design the defenses for a public sign-up and login surface against credential stuffing, fake accounts, and SMS pumping (SMS fraud), including metrics.
36. Design a link-preview feature safely (SSRF-resistant architecture).
37. Design the data protection plan for storing payment card data (PCI scope reduction) and personal data under GDPR (erasure, retention, residency).
38. Produce a threat model (STRIDE table) for the Chapter 12 order pipeline with the top five mitigations.

### D. Debugging / Incident analysis
39. An engineer reports that a logged-in user can view another user's data by changing a number in the URL. Triage: root cause class, immediate mitigation, long-term fixes, and detection.
40. Cloud bill spikes 20× on a weekend; CloudTrail shows API calls from an unknown IP using a CI key. Respond: containment steps, investigation, prevention.
41. Logs show thousands of login attempts per minute, each username tried once, from many IPs. Which attack is this and what controls do you add?
42. A penetration test shows `Access-Control-Allow-Origin` reflecting any origin with credentials allowed. Explain the exploit and the fix.
43. Users report being logged out and sessions reused after a deploy; investigation shows session IDs derived from `Math.random`. Impact and fix.
44. A service's AES-GCM encrypted records after a counter-based nonce scheme restart began producing duplicate nonces. What happened, what is the impact, and how do you remediate historical data?

### E. Interview-style
45. “How would you secure a public REST API?”
46. “Explain how you would store passwords and handle account recovery.”
47. “How do you prevent one tenant from reading another's data?”

### F. Teach-back
48. Explain to a non-engineer why a random-looking URL is not the same as access control, using a hotel analogy.

---

## 22.20 Full answer key

**1.** Confidentiality: only the owner reads invoices. Integrity: the invoice amount can't be altered undetected. Availability: payment API stays up. Authenticity: the caller really is the account holder. Non-repudiation: audit proves who approved a transfer.

**2.** Least privilege: service role limited to one bucket prefix. Defense in depth: WAF + service authz + encryption + monitoring. Complete mediation: ownership check on every read. Fail-closed: authorization service unreachable → deny. Zero trust: services authenticate each other with mTLS and authorize each call.

**3.** Spoofing: credential stuffing → MFA, breach screening, rate limits. Tampering: modified request → TLS, CSRF tokens. Repudiation: "I didn't log in" → audit with IP/device. Info disclosure: user enumeration via different errors → uniform responses/timing. DoS: expensive hashing flood → rate limit, queueing. Elevation: injection/mass assignment in login/profile → parameterized queries, DTOs.

**4.** Slow, salted hashes make each guess expensive and unique per user; salt defeats rainbow tables and shared cracking; pepper (secret key outside the DB) makes a stolen DB insufficient without the pepper.

**5.** Only 10⁶ possibilities: even at 6 guesses/s/core with 10,000× parallelism, it falls in 16 s. Need rate limiting/lockouts on the online channel, longer secrets, hardware-backed verification or MFA, and server-side attempt counters.

**6.** TOTP: moderate, vulnerable to real-time phishing; SMS: weakest (SIM swap); push: usable but fatigue attacks (use number matching); WebAuthn: phishing-resistant (origin-bound public-key), good usability with passkeys.

**7.** Random ≥128-bit ID, `HttpOnly; Secure; SameSite=Lax/Strict; __Host-` prefix, rotate on login/privilege change (fixation), idle and absolute timeouts, server-side invalidation on logout/password change.

**8.** Verify signature (algorithm pinned), `exp/nbf`, `iss`, `aud`, scopes, `kid` from trusted key set. `alg: none` — attacker sets no-signature algorithm; RS/HS confusion — attacker signs with HMAC using the public key as secret when the verifier trusts the header. Fix: pin algorithm/key type.

**9.** The server doesn't look anything up so it can't know of revocation. Mitigations: short TTL + refresh rotation (more auth traffic), denylist/jti cache for sensitive ops (state, lookups), opaque tokens with introspection (latency, central dependency).

**10.** Client creates a random code verifier, sends its hash (challenge) with the authorization request; user authenticates; the client receives a one-time code and redeems it with the verifier; the server checks the hash. PKCE prevents authorization-code interception attacks (a stolen code is useless without the verifier).

**11.** OAuth 2.0: delegated authorization (access tokens). OIDC: identity layer on OAuth (ID token, userinfo). SAML: XML-based federation/SSO, common in enterprises.

**12.** RBAC: simple org roles (admin, viewer). ABAC: policies over attributes (department, amount, time). ReBAC: sharing graphs (docs/folders/groups). Choose RBAC for coarse permissions, ABAC for rich conditions, ReBAC for hierarchical sharing.

**13.** BOLA: accessing other users' objects by ID → ownership check per object. BFLA: low-privilege user calls admin function → function-level authz. Mass assignment: client sets protected fields → DTO allowlists.

**14.** Internal callers and bugs bypass the gateway; services must enforce object-level rules. Confused deputy: privileged service acts for a caller without verifying the caller's authority; fix by evaluating end-user permissions.

**15.** AEAD provides confidentiality and integrity; AAD authenticates unencrypted context (tenant/record) without encrypting it. CBC without a MAC allows undetected tampering and padding-oracle attacks.

**16.** Data encrypted with per-object DEKs; DEKs wrapped by a KEK in KMS. Benefits: bulk data never hits KMS, cheap rotation (re-wrap), per-tenant blast radius/auditability. Crypto-shredding: delete the DEK/KEK so ciphertext is irrecoverable.

**17.** Keystream reuse: C1⊕C2 = P1⊕P2 (lab), plaintext recovery with known/guessed text, plus GCM authentication-key leakage enabling forgery. Prevent via unique nonces (counters/derived per message), key rotation before limits, nonce-misuse-resistant modes.

**18.** Secret manager, workload identity/short-lived credentials, least privilege, rotation, scanning, separate environments, audit. Leak runbook: revoke/rotate immediately, assess usage via logs, scope damage, notify, improve controls.

**19.** SSRF: server fetches attacker-controlled URLs and reaches internal resources. Defense: allowlist/deny private ranges after DNS resolution and per redirect, isolated egress-restricted fetcher, IMDSv2 with hop limit 1, least-privilege instance roles, no raw response return.

**20.** Per-IP limits are evaded by botnets (lab: 60,239 vs 60,165 uncontrolled); per-account lockout is useless because each account is tried once and lockout enables DoS. Effective: MFA, breached-password screening, bot/risk detection, device checks, anomaly monitoring.

**21.** Row-level: `tenant_id` + RLS policies; schema/DB per tenant for stronger isolation; derive tenant from identity. RLS enforces in the database so a forgotten `WHERE` can't leak.

**22.** Log actor, action, object, time, source, outcome, request/trace ID. Never log secrets, tokens, full PANs, passwords. Tamper-evidence via hash chaining/Merkle trees, write-once storage, separate custody.

**23.** Detect (alert/access log), contain (revoke credential, rotate, block sources), investigate (audit logs for usage), eradicate/recover (new creds, patch the leak path), notify (legal/customers), post-incident review and prevention (secrets scanning, short-lived creds).

**24.** (a) 8 chars × log₂94 (6.55) = **52.4 bits** (2⁵².⁴ = 5.6×10¹⁵): at 10¹¹/s: 56,000 s ≈ 15.6 h; at 10⁴/s: 5.6×10¹¹ s ≈ 17,800 years. (b) 4 words: 4 × 12.92 = **51.7 bits** (3.7×10¹⁵): similar. (c) 10 lowercase: 10 × 4.70 = **47 bits** (1.4×10¹⁴): 1,400 s ≈ 23 min at 10¹¹; 1.4×10¹⁰ s ≈ 450 years at 10⁴. (d) 6 digits: **19.9 bits** (10⁶): 10 µs at 10¹¹; 100 s at 10⁴.

**25.** 500 logins/s ÷ 6.1/s/core ≈ **82 cores** for PBKDF2-600k; 500/35.7 ≈ **14 cores** for 100k; 500/305.6 ≈ **1.6 cores** for 10k. Lower iterations cut cost but give attackers 6× (100k vs 600k) to 50× (10k) more guesses per second: size iterations to the highest cost you can afford, offload hashing to a dedicated auth tier, and use rate limits plus MFA.

**26.** p ≈ n²/2⁹⁷. 2²⁰: 2⁴⁰/2⁹⁷ = 2⁻⁵⁷ ≈ 6.9×10⁻¹⁸. 2³⁰: 2⁶⁰/2⁹⁷ = 2⁻³⁷ ≈ 7.3×10⁻¹². 2³²: 2⁶⁴/2⁹⁷ = 2⁻³³ ≈ 1.2×10⁻¹⁰. 2⁴⁰: 2⁸⁰/2⁹⁷ = 2⁻¹⁷ ≈ 7.6×10⁻⁶. Policy: rotate the key before ~2³² messages (NIST limit) or use counters/derived nonces.

**27.** Capacity = 5,000 × 20 × 60 × 12 = 72,000,000 attempts possible; attacker has 8 M pairs → attempts made 8 M. Valid = 8 M × 1.5% = 120,000 takeovers without MFA. With 70% MFA: 36,000. With breach screening 85% (and no MFA): 18,000; both: 120,000 × 0.15 × 0.3 = **5,400**.

**28.** Refresh tokens rotate: when the attacker uses the leaked token (day 2) it is exchanged for a new one; the legitimate user's next refresh presents the *old* token (already used): reuse detected → revoke the entire token family and force re-login. Exposure window: from the theft until the attacker's access token expires or the reuse is detected (the user's next refresh); the attacker may hold access tokens for ≤10 minutes after revocation. Detection is possible only if the user refreshes; otherwise the attacker keeps the session for up to 14 days of rolling refreshes: add device binding and anomaly detection.

**29.** One role per combination: each person picks a subset of a team×env×level space: with 6×5×3 = 90 atomic grants, arbitrary combinations are 2⁹⁰ possible role sets (explosion); practical approach: RBAC with composable roles (team role + environment + level) or ABAC with a few attribute rules (e.g., `user.team == resource.team && level >= required && env allowed`): a handful of rules.

**30.** Bucket starts full (20). In the first second: 20 immediately + ~5 refill = 25 accepted of 100; then 5/s: seconds 2–4 add 15 more (refill) → ≈ **40 accepted in the first 4 seconds** (20 + 4×5). Cost-based: export costs 50 tokens, which exceeds the burst capacity of 20, so it can never run: raise capacity for that class or use a separate bucket (e.g., 100 capacity, 1 token/s) so an export is allowed at most once per ~50 s sustained.

**31.** Replayable for the full 5-minute tolerance. Eliminate replay by storing each webhook ID/nonce for the tolerance window and rejecting repeats. 10,000/min × 5 min = 50,000 IDs × ~64 B ≈ **3.2 MB** (trivial; Redis `SET NX EX 300`).

**32.** 2 GB/s ÷ 3 GB/s/core ≈ **0.7 core** (use 1–2 cores plus headroom). KMS unwraps: without caching 20,000/s (likely over KMS quotas/costs); with 5-minute DEK caching: 2,000 distinct DEKs / 300 s ≈ **6.7 unwraps/s**, a 3,000× reduction. Trade-off: cached DEKs live in memory (protect memory; shorter TTL for high sensitivity).

**33 (sketch).** Central IdP (OIDC), authorization code + PKCE for web/mobile, API keys → OAuth client credentials with scopes for public API; access tokens 10 min (JWT, RS256/EdDSA, `aud` per API), refresh rotation with reuse detection; tenant ID derived from token claims; RLS in DB; mTLS/workload identity for service-to-service; propagate end-user context via signed internal tokens; revocation via short TTL + denylist for admin actions.

**34 (sketch).** Workload identity (SPIFFE/IRSA) → secret manager issues dynamic credentials with TTL; KMS root→KEK per environment→DEK per service/tenant; automated rotation; CI uses OIDC federation; scanning; break-glass role with approvals and alerting; inventory and access reviews.

**35 (sketch).** Edge rate limits + bot detection; breached-password screening; MFA/passkey enrollment prompts; device/ASN risk scoring; email/phone verification with velocity limits; SMS cost caps and country allowlists to stop SMS pumping; metrics: login success ratio by ASN, new-account rate, SMS spend per signup, takeover rate.

**36 (sketch).** Fetch service in an isolated network with default-deny egress except the internet through a proxy; resolve DNS and block private/metadata ranges at connect time and on redirects; size/time limits; no credentials; parse HTML in a sandbox; return sanitized preview data; IMDSv2 hop limit 1; no sensitive IAM role.

**37 (sketch).** Card data: hosted fields/provider tokenization so PANs never enter your systems; store tokens only. GDPR: data inventory, per-region storage, retention schedules, per-subject keys for erasure, deletion propagation to indexes/caches/analytics, consent and DSAR tooling, breach runbook.

**38 (sketch).** STRIDE: spoofed webhooks (HMAC verification), tampered messages (signed/validated payloads), repudiation of orders (audit trail), info disclosure in logs/DLQ (redaction), DoS via queue flooding (rate limits, bounded queues), elevation via worker roles (least-privilege IAM). Top mitigations: authn/z on every endpoint, idempotency + signature verification, encryption, least privilege, monitoring.

**39.** Class: BOLA/IDOR. Mitigation: add ownership check/deny by default immediately (hotfix), review similar endpoints; long term: centralized authz layer, tests (cross-user access), opaque IDs, code review checklist; detection: log access to other users' objects, alert on enumeration patterns (sequential IDs, many 403s/404s).

**40.** Contain: disable/rotate the CI key, revoke sessions, quarantine resources, block IP; investigate with audit logs (what was created/read, persistence like new IAM users); prevent: OIDC federation for CI (no static keys), scoped roles, budgets/alarms, secret scanning, anomaly alerts.

**41.** Credential stuffing. Add breach-password screening, MFA, bot detection/risk scoring, rate limits by ASN/device/behavior, CAPTCHA on anomalies, monitor success ratio, notify users on suspicious logins.

**42.** Any malicious site can make a victim's browser send credentialed cross-origin requests and read responses (data theft). Fix: allowlist specific origins; never reflect arbitrary Origin with credentials; use CSRF/SameSite defenses.

**43.** Predictable session IDs → attackers can guess/derive sessions (hijacking). Fix: CSPRNG-generated IDs ≥128 bits, invalidate all existing sessions, force re-login, add detection.

**44.** The counter wasn't persisted/recovered, so after restart it restarted from an old value, reusing nonces under the same key. Impact: plaintext recovery and potential forgery for records sharing a nonce. Remediate: stop encryption with that key, rotate keys, re-encrypt affected data, find reused (key, nonce) pairs from stored records, persist counters or use random nonces/derived keys.

**45 (sketch).** HTTPS/TLS 1.3, OAuth2/OIDC tokens (short-lived), scopes, per-object authorization, input validation + schema, rate limits/quotas, WAF, logging + monitoring, secrets management, versioning/deprecation, DoS protections, API inventory, security testing.

**46 (sketch).** Argon2id/bcrypt/PBKDF2 with per-user salt (+ pepper in KMS), breach screening, MFA, rate limiting; recovery via verified email/MFA with short-lived single-use tokens, cooldown, notifications, no security questions, support-desk verification procedures.

**47 (sketch).** Tenant from identity, RLS and tenant-scoped data access layer, tenant in cache keys/prefixes/queues, per-tenant keys, tests for cross-tenant access, quotas, audit; stronger isolation for sensitive tenants (DB/account per tenant).

**48.** A hotel with room numbers on the doors: a random-looking room number makes it hard to guess which room is yours, but if the front desk gives out a key to anyone who says a room number, anyone who learns the number (from a receipt, an email, a photo) can open it. Real security is the front desk checking your ID each time.

---

## 22.21 Flashcards

| Front | Back |
|---|---|
| CIA | Confidentiality, integrity, availability |
| Principles | Least privilege, defense in depth, complete mediation, fail closed, zero trust |
| STRIDE | Spoofing, Tampering, Repudiation, Info disclosure, DoS, Elevation |
| Password storage | Salted slow memory-hard hash (Argon2id/bcrypt/scrypt/PBKDF2 600k) |
| Fast hash cost | SHA-256: 8.2 M guesses/s/core (lab) vs PBKDF2-600k: 6.1/s |
| MFA best | WebAuthn/passkeys (phishing-resistant) |
| Cookie flags | HttpOnly, Secure, SameSite, __Host- |
| JWT verify | Pin alg; check sig, exp, iss, aud, scopes; no revocation without state |
| OAuth flow | Authorization code + PKCE; OIDC for identity |
| Service identity | mTLS, SPIFFE/workload identity, short-lived certs |
| Credential stuffing | Per-IP limits fail vs botnets; lockout useless; MFA + breach screening |
| Lab stuffing | 60 k takeovers → 4.7 k with MFA 60% + screening |
| Authorization | Deny by default; object/function/property checks in every service |
| BOLA lab | 99,993/100,000 readable without ownership check |
| Opaque IDs | Reduce guessing; not authorization (leaked ids still readable) |
| AEAD | AES-GCM/ChaCha20-Poly1305; AAD binds context |
| Nonce reuse | C1⊕C2 = P1⊕P2; rotate keys before ~2³² msgs |
| Envelope encryption | DEK per object, wrapped by KEK in KMS; crypto-shredding |
| Secrets | None in code; secret manager; short-lived; rotation |
| SSRF | Block private/metadata ranges after DNS; IMDSv2; egress allowlist |
| Injection | Parameterized queries |
| Multi-tenant | Tenant from identity; RLS; tenant in cache keys |

## 22.22 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Authentication | Who are you | Verifying identity | Trust | Login, MFA |
| Authorization | What may you do | Access decision | Least privilege | RBAC |
| Threat model | Plan for attacks | Systematic analysis of assets/threats | Design-time security | STRIDE |
| Trust boundary | Where trust changes | Edge between differently trusted zones | Focus controls | Internet↔API |
| Salt / pepper | Per-user / secret extra input | Hash inputs defeating precomputation / DB theft | Password security | 16 B random |
| Argon2id / bcrypt / PBKDF2 | Slow password hashes | KDFs with tunable cost | Resist cracking | cost 12 |
| MFA / WebAuthn | Second factor / passkeys | Multi-factor; origin-bound public-key credentials | Account protection | Passkey |
| Session fixation | Forced session ID | Attacker sets victim's session ID | Rotate on login | — |
| JWT | Signed token | Header.payload.signature | Stateless auth | Access token |
| JWKS | Public key set | Published verification keys | Key rotation | `/.well-known/jwks.json` |
| OAuth 2.0 / OIDC | Delegation / identity | Authorization framework / identity layer | SSO | Google login |
| PKCE | Code proof | Verifier/challenge binding for code flow | Prevent code theft | S256 |
| mTLS | Two-way TLS | Both sides present certificates | Service identity | Mesh |
| SPIFFE/SPIRE | Workload identity | Standard + runtime issuing SVIDs | No static creds | spiffe://… |
| RBAC / ABAC / ReBAC | Role / attribute / relationship authz | Access models | Policy | Zanzibar |
| BOLA / IDOR | Object-level authz flaw | Access to others' objects by ID | #1 API risk | `/orders/42` |
| Confused deputy | Misused privilege | Privileged component acts for unauthorized caller | Authz bug class | Proxy service |
| AEAD / AAD | Authenticated encryption / context data | Encrypt+authenticate; authenticated extra data | Integrity | AES-GCM |
| Nonce | Number used once | Unique per-encryption value | Prevent keystream reuse | 96-bit |
| KMS / HSM | Key service / hardware | Managed key storage and operations | Protect keys | Cloud KMS |
| Envelope encryption | Wrapped data keys | DEKs encrypted by KEK | Scale + rotation | Per-tenant DEK |
| Crypto-shredding | Erase by deleting key | Destroying key renders data unreadable | Erasure | GDPR |
| SSRF | Server makes attacker's request | Server-side request forgery | Cloud credential theft | Metadata service |
| WAF | Web firewall | L7 filtering of attacks | Edge defense | OWASP CRS |
| RLS | Row-level security | DB-enforced row filters | Tenant isolation | Postgres policy |
| SIEM | Security log analytics | Aggregates events and detections | Detection | Splunk |
| SBOM / SLSA | Component list / supply-chain levels | Software provenance | Supply chain | cosign |

---

**Next: Chapter 23: Cloud, Containers, and Infrastructure.** Virtualization and containers, Kubernetes architecture (scheduling, requests/limits, HPA), infrastructure as code, cloud networking, regions/AZs, managed services, cost engineering (measured scheduler packing, spot economics, autoscaler flapping, network costs), and multi-cloud/portability trade-offs.
