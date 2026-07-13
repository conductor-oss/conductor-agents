# Authentication & SSO

The harness carries a credential you supply on every request — it never automates the IdP login itself.

## Supplying credentials

| Target auth | How to supply it |
|---|---|
| API key / service account | `--id 'u=key:<K>,secret:<S>,tokenurl:https://app/api/token'` — re-exchanged locally each run (best for long campaigns). `./scan` uses `--auth-key/--auth-secret/--token-url`. |
| Bearer/JWT you already have | `--id 'u=token:<JWT>'` (or `--auth-token` for `./scan`). Use `header:`/`scheme:` to override `Authorization: Bearer`. |
| SSO (Google/Okta/SAML) | `./sso-capture` — log in once in a real browser, hand off the result (see below). |

## SSO in one step

`./sso-capture` opens a real browser; you complete the login; it sniffs the auth header your app sends (scoped to the target domain — IdP cookies are ignored), and writes a credential file:

```bash
./sso-capture https://app.example.com --label userA
#   ✓ captured bearer-sniffed credential for 'userA' → state/sessions/userA.json

./assess https://app.example.com --authorized --id 'userA=session:state/sessions/userA.json'
./scan   https://app.example.com --authorized --session state/sessions/userA.json
```

It captures the strongest available credential — sniffed bearer token → localStorage JWT → session cookie — and saves the full browser `storage_state`.

!!! note "Token expiry"
    SSO access tokens expire; prefer API-key credentials for long runs and re-run `./sso-capture` to refresh.

For interactive UI exploitation with an already-logged-in browser, see `make chrome` + `SC_CDP_URL` in [Deployment modes](deployment.md).
