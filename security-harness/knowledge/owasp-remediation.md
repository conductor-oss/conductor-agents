# Vulnerability remediation knowledge base

Curated remediation guidance + authoritative references, keyed by OWASP Top 10
(2021) category. The `triage` and `report` LLM tasks ground their remediation
advice in these references. (For vector-RAG grounding at scale, see the note in
the README — it requires a configured vectorDB integration; this static KB is the
zero-dependency default.)

## A01:2021 – Broken Access Control
Enforce authorization server-side on every request (deny by default). Never rely
on hidden fields/UI. Use object-level checks (the record belongs to the caller)
to prevent IDOR/BOLA. CSRF tokens on state-changing requests.
Ref: OWASP Cheat Sheet — Access Control; Authorization; CSRF Prevention.

## A02:2021 – Cryptographic Failures
TLS everywhere (HSTS); never serve auth/session over HTTP. Encrypt sensitive data
at rest; use strong, salted password hashing (argon2/bcrypt). No secrets in code.
Ref: OWASP Cheat Sheet — Transport Layer Security; Password Storage.

## A03:2021 – Injection (SQLi, XSS, command, etc.)
Parameterize all queries (prepared statements/ORM). Context-aware output encoding
+ a strict Content-Security-Policy for XSS. Never pass user input to a shell;
use argument arrays / allow-lists. Validate input server-side.
Ref: OWASP Cheat Sheet — SQL Injection Prevention; Cross Site Scripting Prevention;
OS Command Injection Defense.

## A04:2021 – Insecure Design
Threat-model the feature; add rate limits and abuse cases; fail securely.

## A05:2021 – Security Misconfiguration
Set security headers (CSP, HSTS, X-Content-Type-Options, Referrer-Policy,
Permissions-Policy). Disable directory listing, verbose errors, default creds,
and unused features. Lock down CORS (no reflected Origin with credentials).
Ref: OWASP Cheat Sheet — HTTP Security Response Headers.

## A06:2021 – Vulnerable & Outdated Components
Inventory dependencies; patch known CVEs; remove unused packages; pin versions.

## A07:2021 – Identification & Authentication Failures
MFA; rotate/secure session tokens (Secure, HttpOnly, SameSite); throttle login;
no hardcoded credentials. Ref: OWASP Cheat Sheet — Authentication; Session Management.

## A08:2021 – Software & Data Integrity Failures
Verify integrity of updates/CI artifacts; avoid insecure deserialization.

## A09:2021 – Security Logging & Monitoring Failures
Log security-relevant events; alert on anomalies; protect logs.

## A10:2021 – Server-Side Request Forgery (SSRF)
Allow-list outbound destinations; block internal ranges/metadata endpoints;
do not follow user-controlled redirects server-side.
Ref: OWASP Cheat Sheet — Server Side Request Forgery Prevention.
