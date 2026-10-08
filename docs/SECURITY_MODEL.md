# Security and privacy model

This is a sanitized source snapshot intended for code review and recruitment. Do **not** treat a public repository as a place for real media, API keys, OAuth callbacks, chat IDs, transcripts, incident reports, or cloud deployment identifiers.

## Confidential information

- Inject Gemini and Telegram credentials through environment variables or a suitable secrets manager.
- Drive authentication uses user OAuth. Do not commit `credentials.json`, `token.json`, refresh tokens, client secrets, or full OAuth callback URLs.
- Cloud service identities should rely on Application Default Credentials and least-privilege IAM. Do not package service-account JSON keys.
- Never upload private audio or generated documents as test fixtures.

## Access boundaries

- Restrict Telegram operations to authorized chat identities.
- Restrict any deployed dashboard with a trusted identity-aware access layer (for example IAP), not simply a user selector.
- A dashboard's user filter is **not** authentication or authorization.
- Keep status data minimal. Transcript contents and secrets must not be serialized to the dashboard.
- Use separate read-only identity/permissions for monitoring interfaces, rather than granting job-write privileges.

## Operational protections

- Avoid printing secret values in logs, CI, or issues.
- Separate production configuration from code and keep it private.
- Review cloud permissions and runtime exposure before deployment.
- Rotate a credential immediately if exposure is suspected.
- Run secret scanning before publication and after meaningful changes.
- Pin/update dependencies and review container base images.

## Source snapshot limitations

This portfolio removes the original repository's history and production-specific documents. It does not include a deployed public demo, proof of an independent production penetration test, or real infrastructure credentials. CI provides automated regression confidence but does not guarantee security.
